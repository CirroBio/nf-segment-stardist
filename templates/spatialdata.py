#!/usr/bin/env python3

from geopandas import GeoDataFrame
from numpy import array
from shapely import Polygon
from spatialdata.models import ShapesModel
from spatialdata.transformations.transformations import Scale
from typing import List
import anndata as ad
from spatialdata.models import TableModel
import logging
import json
import gzip

logger = logging.getLogger(str(__name__))


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
    px_size=1.0
) -> GeoDataFrame:

    logger.info(f"Parsing GeoJson - {kw} (px_size={px_size})")

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
        [1.0 / px_size, 1.0 / px_size],
        axes=("x", "y")
    )

    return ShapesModel.parse(
        geo_df,
        transformations={"global": scale}
    )


def main(
    anndata = "${anndata}",
    cells_geo_json = "${cells_geo_json}",
    image = "${image}",
    px_size = ${px_size}
):
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
            px_size=px_size
        ),
        nucleus=parse_geo_json(
            geo_json,
            "nucleusGeometry",
            px_size=px_size
        )
    )


if __name__ == "__main__":
    main()