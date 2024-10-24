#!/usr/bin/env python3

import click
from geopandas import GeoDataFrame
from numpy import array
from rasterio.features import rasterize
from shapely import Polygon, Point
from skimage.io import imread as sk_imread
from spatialdata import SpatialData
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from multiscale_spatial_image import to_multiscale
from pathlib import Path
from spatialdata.models import ShapesModel, TableModel, Image2DModel
from spatialdata.transformations.transformations import Scale
from tifffile import TiffFile, TiffPage
from typing import List, Mapping, Tuple, Union
from xml.etree import ElementTree
import anndata as ad
import dask.array as da
import gzip
import json
import logging
import sys

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


def read_table(fp: str) -> TableModel:
    """
    Read in the tablular elements of the spatial data
    and convert to a TableModel object.
    """
    logger.info(f"Reading in {fp} as AnnData")
    adata = ad.read_h5ad(fp)
    adata.obs["region"] = "cell_boundaries"
    adata.obs["region"] = adata.obs["region"].astype("category")

    return TableModel.parse(
        adata,
        region="cell_boundaries",
        region_key="region",
        instance_key="Object ID"
    )


def parse_geo_json(
    geo_json: List[dict],
    kw: str,
    pixel_size=1.0
) -> GeoDataFrame:

    logger.info(f"Parsing GeoJson - {kw} (pixel_size={pixel_size})")

    geo_df = (
        GeoDataFrame([
            dict(
                id=cell["id"],
                geometry=Polygon(
                    array(
                        cell[kw]["coordinates"][0]
                    )
                )
            )
            for cell in geo_json
        ])
        .set_index("id")
    )
    scale = Scale(
        [1.0 / pixel_size, 1.0 / pixel_size],
        axes=("x", "y")
    )

    return ShapesModel.parse(
        geo_df,
        transformations={"global": scale}
    )


def make_spatial_points(
    table: ad.AnnData,
    pixel_size=1.0,
    instance_key="cell_id"
):

    scale = Scale(
        [1.0 / pixel_size, 1.0 / pixel_size],
        axes=("x", "y")
    )

    # Create a GeoDataFrame to encode the points
    logger.info("Converting spatial points to GeoDataFrame")
    geo_df = GeoDataFrame(
        geometry=[
            Point(x / pixel_size, y / pixel_size)
            for x, y in table.obsm["spatial"]
        ]
    )
    geo_df = geo_df.assign(radius=20)
    geo_df.index = table.obs[instance_key]

    return ShapesModel.parse(geo_df, transformations={"global": scale})


def read_tif_channel_names(tmp_file: str, n_channels: int) -> List[str]:
    """Parse channel names from a TIF file."""

    # Try to parse QPTIFF metadata
    logger.info("Parsing QPTIFF metadata")
    channel_names = parse_qptiff_metadata(tmp_file)

    # If no names were found
    if channel_names is None:
        logger.info("No channel names found from QPTIFF format")

        # Try to parse OME metadata
        logger.info("Parsing OME-TIFF metadata")
        channel_names = parse_ome_metadata(tmp_file)

    if channel_names is None:
        logger.info("No OME-TIFF metadata found")

    elif len(channel_names) > 0:
        logger.info("Parsed channel names")
        for cname in channel_names:
            logger.info(cname)

    # Fallback if metadata was not parsed appropriately
    if channel_names is None or len(channel_names) != n_channels:

        logger.info("Falling back to numerically indexed channels")

        # The channels are just named numerically (1-indexed)
        channel_names = [
            str(ix + 1)
            for ix in range(n_channels)
        ]

    return channel_names


def parse_qptiff_metadata(tmp_file) -> List[str]:
    """Parse channel names from QPTIFF."""

    with TiffFile(tmp_file) as tif:
        channel_names = [
            parse_qptiff_metadata_page(page)
            for page in tif.series[0].pages
        ]

    # If metadata could not be parsed, return None
    for cn in channel_names:
        if cn is None:
            return None

    return channel_names


def parse_qptiff_metadata_page(page: TiffPage) -> Union[str, None]:
    """Parse a single channel name from QPTIFF."""

    # Catch errors when there is no page.description
    if not hasattr(page, "description"):
        return None

    # Parse the XML
    try:
        dat = ElementTree.fromstring(page.description)
    except ElementTree.ParseError:
        logger.info("Could not parse XML from file")
        return None

    # Try different keywords
    for kw in [
        "Biomarker",
        "Name"
    ]:
        elem = dat.find(kw)
        if elem is not None:
            return elem.text


def _parse_ome_xml(tmp_file: Path) -> Union[None, List[str]]:
    """Try to read the OME-XML metadata from a TIFF file."""

    # Try to read OME metadata
    with TiffFile(tmp_file) as tif:
        ome_metadata = tif.ome_metadata

    if ome_metadata is None:
        return None

    # Parse the metadata
    try:
        root = ElementTree.fromstring(ome_metadata)
    except ElementTree.ParseError:
        logger.info("Could not parse XML from file")
        return None
    
    return root
    

def parse_ome_metadata(tmp_file: Path) -> Union[None, List[str]]:
    """Parse channel names from OME-TIFF."""

    # Try to read OME metadata
    root = _parse_ome_xml(tmp_file)
    if root is None:
        return

    # Try to get the channel names using the OME schema
    channel_names = [
        elem.attrib["Name"]
        for elem in root.iter("{http://www.openmicroscopy.org/Schemas/OME/2016-06}Channel")
        if elem.attrib.get("Name") is not None
    ]
    if len(channel_names) > 0:
        return channel_names

    # Fall back to any list of Names
    return _find_name_list(root)


def _find_name_list(elem: ElementTree.Element) -> Union[None, List[str]]:
    names = [
        ch.attrib["Name"]
        for ch in elem
        if ch.attrib.get("Name") is not None
    ]
    if len(names) > 0:
        return names
    else:
        for ch in elem:
            if _find_name_list(ch) is not None:
                return _find_name_list(ch)


def downscale_image(
    image,
    scale_factor=2,
    min_px=400,
    chunk_x=300,
    chunk_y=300,
    chunk_c=1
) -> MultiscaleSpatialImage:

    # Pick the number of scales so that the smallest
    # is no smaller than min_px
    scales = [scale_factor]
    while (
        min(image.shape[1], image.shape[2]) /
        (scale_factor**len(scales))
    ) > min_px:
        scales.append(scale_factor)
    scales_str = ', '.join(map(json.dumps, scales))

    # Convert to multiscale
    # Set chunks on each level of scale
    chunks = dict(c=chunk_c, x=chunk_x, y=chunk_y)
    chunks_str = json.dumps(chunks)
    params = f"scales={scales_str}; chunks={chunks_str}"
    logger.info(f"Converting to multiscale ({params})")
    return to_multiscale(
        image,
        scales,
        chunks=chunks
    )


def read_tif(
    tmp_file,
    table: ad.AnnData,
    shapes:  Mapping[str, GeoDataFrame],
    masks: Mapping[str, GeoDataFrame],
    min_px=400,
    scale_factor=2,
    chunk_x=300,
    chunk_y=300,
    chunk_c=1,
    dataset_name="Image"
) -> Tuple[SpatialData, dict]:
    """
    Read in a TIF file
    """

    logger.info(f"Reading TIF image from {tmp_file}")

    # Read the image
    try:
        image = sk_imread(tmp_file, plugin="tifffile")
    except MemoryError as e:
        logger.info(str(e))
        logger.info("Exiting: 137")
        sys.exit(137)
    logger.info("Converting to array")
    image = da.from_array(image)

    # The array must have at least two dimensions
    assert len(image.shape) >= 2, "Image must have at least two dimensions"

    # If there are more than three dimensions
    if len(image.shape) > 3:
        # One of the dimensions must have zero length
        assert min(image.shape) == 1, "Can only display three dimensions"

        # Remove all of the zero length dimensions
        logger.info("Squeezing extra dimensions")
        image = image.squeeze()

    # If the image only has two dimensions
    if len(image.shape) == 2:
        # Add a color dimension
        logger.info("Adding extra color dimension")
        image = da.expand_dims(image, axis=0)

    # At this point there are only three dimensions
    assert len(image.shape) == 3, "Can only display three dimensions"

    # Find the shortest dimension (which we assume is color)
    cax = image.shape.index(min(image.shape))

    # If it's not the first one, move it
    if cax != 0:
        logger.info(f"Moving axis {cax} to position 0")
        image = da.moveaxis(image, cax, 0)

    # Read the channel names
    logger.info(f"Parsing channel names from {tmp_file}")
    channel_names = read_tif_channel_names(tmp_file, image.shape[0])
    for cname in channel_names:
        logger.info(cname)

    # If there are masks
    mask_channels = dict()
    if masks is not None:

        # Add the masks as image channels
        for mask_name, mask_geo in masks.items():

            mask_channels[mask_name] = image.shape[0]

            # Add a new color channel with the rasterized shapes
            image = da.concatenate(
                [
                    image,
                    da.expand_dims(
                        rasterize(
                            mask_geo.geometry,
                            default_value=1,
                            fill=0,
                            out_shape=image.shape[1:],
                            all_touched=True,
                            dtype=image.dtype
                        ),
                        axis=0
                    )
                ],
                axis=0
            )

            # Add the channel name
            channel_names.append(mask_name)

    # Convert the image to multiscale and build an
    # image model which can be used in a SpatialData object
    image = format_spatial_image(
        image,
        channel_names,
        scale_factor,
        min_px,
        chunks=dict(
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_c=chunk_c
        )
    )

    # Convert to SpatialData
    logger.info("Converting to SpatialData")
    sdata = SpatialData(
        images=dict(image=image),
        shapes=dict(shapes=shapes),
        tables=dict(table=table)
    )

    # Show the first three channels
    init_channels = dict(zip(
        ["channel_a", "channel_b", "channel_c"],
        range(len(channel_names))
    ))
    for kw, val in init_channels.items():
        logger.info(f"{kw}: {channel_names[val]}")

    # Set up keyword arguments for the configuration
    vt_kwargs = dict(
        dataset_name=dataset_name,
        image_key="image",
        rgb_a=[0, 0, 255],
        rgb_b=[0, 255, 0],
        rgb_c=[255, 0, 0],
        mask_channels=mask_channels,
        **init_channels
    )

    return sdata, vt_kwargs


def format_spatial_image(
    image,
    channel_names,
    scale_factor,
    min_px,
    chunks
):

    # Build the image model
    logger.info("Building Image2DModel")
    image = Image2DModel.parse(
        image,
        dims=('c', 'y', 'x'),
        c_coords=channel_names
    )

    # Convert to multiscale
    # Function will pick the number of scales so that
    # the smallest is no smaller than min_px.
    # Set chunks on each level of scale.
    image: MultiscaleSpatialImage = (
        downscale_image(
            image,
            min_px=min_px,
            scale_factor=scale_factor,
            **chunks
        )
    )

    return image


@click.command()
@click.option("--anndata", type=str, help="Path to the AnnData file")
@click.option("--cells_geo_json", type=str, help="Path to the cell geometry JSON")
@click.option("--image", type=str, help="Path to the image file")
@click.option("--pixel_size", type=float, help="Pixel size in microns")
def main(anndata, cells_geo_json, image, pixel_size):

    # Read in the AnnData object
    logger.info(f"Reading in {anndata}")
    table = read_table(anndata)

    # Read in the cell geometry
    logger.info(f"Reading in {cells_geo_json}")
    geo_json = json.load(gzip.open(cells_geo_json, "r"))

    # Parse the outlines of the cells and nuclei, and the centroids
    masks = dict(
        cell=parse_geo_json(
            geo_json,
            "geometry",
            pixel_size=pixel_size
        ),
        nucleus=parse_geo_json(
            geo_json,
            "nucleusGeometry",
            pixel_size=pixel_size
        )
    )

    shapes = dict(
        centroids=make_spatial_points(
            table,
            pixel_size=pixel_size,
            instance_key="Object ID"
        )
    )

    # Read in the image, adding the annotated shapes
    # and table to the SpatialData object
    logger.info("Reading in the image")
    sdata, vt_kwargs = read_tif(
        image,
        table=table,
        shapes=shapes,
        masks=masks,
        dataset_name="StarDist Processed Image"
    )

    # Save to Zarr
    logger.info("Saving to Zarr")
    sdata.to_zarr("spatialdata.zarr")

    # Save the spatialdata kwargs to JSON
    logger.info("Saving spatialdata kwargs to JSON")
    with open("spatialdata.kwargs.json", "w") as f:
        json.dump(vt_kwargs, f, indent=4)


if __name__ == "__main__":
    main()
