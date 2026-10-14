# Development image for gl-tracker.
#
# Carries the full dependency stack needed to go from a radarlib CFRadial
# NetCDF all the way to the tracker's SpatiaLite output:
#
#   CFRadial .nc --(radarlib apply_geometry)--> Cartesian grid .nc
#                --(gl-tracker pipeline)------> systems-db.sqlite
#
# radarlib itself is NOT baked in. It is picked up from a live bind mount via
# PYTHONPATH so edits in the radarlib repo take effect without rebuilding.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# System libraries that have no pip equivalent:
#   libgdal-dev              osgeo bindings — tathu's core dependency
#   libsqlite3-mod-spatialite  tathu.io.spatialite loads it with load_extension;
#                            without it the output database is never created
#   libspatialindex-dev      rtree, used by tathu.tracking.system
#   libgeos-dev              shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      git \
      gdal-bin \
      libgdal-dev \
      libgeos-dev \
      libspatialindex-dev \
      libsqlite3-mod-spatialite \
      proj-bin \
    && rm -rf /var/lib/apt/lists/*

# The GDAL build needs these to find its headers
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal \
    C_INCLUDE_PATH=/usr/include/gdal

# Python GDAL bindings must match the system GDAL version exactly
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir "GDAL==$(gdal-config --version)"

# TATHU is not published on PyPI (github.com/uba/tathu). Upstream installs it
# with --no-deps on top of a conda env; we do the same and then supply only the
# subset that gl-tracker's import path actually reaches. The declared extras
# (cartopy, geopandas, s3fs, psycopg2, requests, tqdm, aenum) serve tathu's
# satellite/download features, which this project never imports.
RUN pip install --no-cache-dir --no-deps git+https://github.com/uba/tathu.git \
 && pip install --no-cache-dir \
      numpy \
      scipy \
      scikit-image \
      opencv-python-headless \
      affine \
      pathos \
      rasterstats \
      rtree \
      shapely \
      netCDF4

# radarlib's dependencies, for the gridding step. radarlib itself arrives on
# PYTHONPATH from the bind mount — see .devcontainer/devcontainer.json.
RUN pip install --no-cache-dir \
      arm-pyart \
      pyproj \
      pandas \
      pytz \
      cachetools \
      xarray \
      psutil \
      matplotlib

WORKDIR /workspace/gl-tracker

CMD ["bash"]
