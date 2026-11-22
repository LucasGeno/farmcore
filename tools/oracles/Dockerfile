# Optional oracle container: installs copyleft reference implementations at build time and runs
# FarmCore's own generator scripts against them. Nothing here is packaged, nothing runs in CI,
# and no copyleft source is copied into the repository. Build and run with tools/oracles/run.sh,
# which builds one target per oracle.
#
# rocker/r-ver pins CRAN to a dated Posit Package Manager snapshot for its R version.

# --- degday -------------------------------------------------------------------------------
FROM rocker/r-ver:4.4.2 AS degday

# degday (Andy Lyons, Regents of the University of California), GPL (>= 3).
# Installed from the CRAN tarball, verified by checksum, so the fixtures name an exact version.
ARG DEGDAY_VERSION=0.4.0
ARG DEGDAY_SHA256=1e0ca4504d92c78641defe36d4df067e17d9e49ea969aa48a2bb40680afd451d
ENV DEGDAY_VERSION=${DEGDAY_VERSION} DEGDAY_SHA256=${DEGDAY_SHA256}

# CRAN moves a package's tarball to Archive/ once a newer version is published.
RUN Rscript -e 'install.packages(c("crayon", "magrittr", "zoo", "jsonlite"))' \
 && Rscript -e 'v <- Sys.getenv("DEGDAY_VERSION"); f <- sprintf("degday_%s.tar.gz", v); \
      urls <- c(paste0("https://cran.r-project.org/src/contrib/", f), \
                sprintf("https://cran.r-project.org/src/contrib/Archive/degday/%s", f)); \
      for (u in urls) if (!inherits(try(download.file(u, "/tmp/degday.tar.gz", quiet = TRUE)), \
        "try-error")) quit(status = 0); quit(status = 1)' \
 && echo "${DEGDAY_SHA256}  /tmp/degday.tar.gz" | sha256sum -c - \
 && R CMD INSTALL /tmp/degday.tar.gz \
 && rm /tmp/degday.tar.gz

COPY degday/generate.R /oracles/degday/generate.R
WORKDIR /oracles

# --- viticolR -----------------------------------------------------------------------------
FROM rocker/r-ver:4.4.2 AS viticolr

# viticolR (Paul Melloy), GPL (>= 3), and its dependency epiphytoolR (Paul Melloy), MIT.
# Neither has tags or releases: both are pinned by commit and the GitHub tarball's checksum.
ARG VITICOLR_COMMIT=f32eb0abe02d49882eefe2c957e90776fac74b59
ARG VITICOLR_SHA256=03a06d5073bdac85a6510dc1c5812dfdf19bc4983e5102cb6c391aa557f9693b
ARG EPIPHYTOOLR_COMMIT=3cab18e878f819f882e25a2c5c51112910864967
ARG EPIPHYTOOLR_SHA256=c05a07a111fd659879d379c656b793b44ae704c1f453e7edb2211fafe0c6c309
ENV VITICOLR_COMMIT=${VITICOLR_COMMIT} VITICOLR_SHA256=${VITICOLR_SHA256} \
    EPIPHYTOOLR_COMMIT=${EPIPHYTOOLR_COMMIT} EPIPHYTOOLR_SHA256=${EPIPHYTOOLR_SHA256}

# epiphytoolR imports sf, which links GDAL, GEOS, PROJ and udunits, and calls lubridate
# without declaring it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgdal-dev libgeos-dev libproj-dev libudunits2-dev libssl-dev \
 && rm -rf /var/lib/apt/lists/* \
 && Rscript -e 'install.packages(c("data.table", "circular", "sf", "units", "geosphere", \
      "jsonlite", "ggplot2", "Rdpack", "lubridate"))' \
 && Rscript -e 'stopifnot(all(c("sf", "data.table", "ggplot2", "Rdpack") %in% rownames(installed.packages())))'

# viticolR's repository root carries a developer .Rprofile that loads devtools; skip it.
RUN Rscript -e 'download.file(sprintf("https://codeload.github.com/PaulMelloy/epiphytoolR/tar.gz/%s", \
      Sys.getenv("EPIPHYTOOLR_COMMIT")), "/tmp/epiphytoolR.tar.gz", quiet = TRUE); \
      download.file(sprintf("https://codeload.github.com/PaulMelloy/viticolR/tar.gz/%s", \
      Sys.getenv("VITICOLR_COMMIT")), "/tmp/viticolR.tar.gz", quiet = TRUE)' \
 && echo "${EPIPHYTOOLR_SHA256}  /tmp/epiphytoolR.tar.gz" | sha256sum -c - \
 && echo "${VITICOLR_SHA256}  /tmp/viticolR.tar.gz" | sha256sum -c - \
 && R_PROFILE_USER=/dev/null R CMD INSTALL /tmp/epiphytoolR.tar.gz \
 && R_PROFILE_USER=/dev/null R CMD INSTALL /tmp/viticolR.tar.gz \
 && rm /tmp/*.tar.gz

COPY viticolr/generate.R /oracles/viticolr/generate.R
WORKDIR /oracles

# --- plasmopy -----------------------------------------------------------------------------
FROM python:3.12.8-slim-bookworm AS plasmopy

# plasmopy (Livio Ruzzante, Agroscope), AGPL-3.0. No tags or releases: pinned by commit and the
# GitHub tarball's checksum. It is a program, not a library: the generator runs src/main.py.
ARG PLASMOPY_COMMIT=2fea0cb3dfc9e5892f231328a03447ea381dd3f8
ARG PLASMOPY_SHA256=924133672435ac5209bbf9cb0f89d1c520cb5f7e5031020bdbb0d21cd26d1a36
ENV PLASMOPY_COMMIT=${PLASMOPY_COMMIT} PLASMOPY_SHA256=${PLASMOPY_SHA256}

# The runtime imports of src/ (upstream has no lock file); versions follow its pyproject pins.
RUN pip install --no-cache-dir \
      hydra-core==1.3.2 pandas==2.2.3 numpy==1.26.2 matplotlib==3.9.2 plotly==5.22.0 \
      suntimes==1.1.2 pytz==2025.1 tqdm==4.67.1 requests==2.32.3 \
 && python -c "import urllib.request as u; u.urlretrieve( \
      'https://codeload.github.com/agroscope-ch/plasmopy/tar.gz/${PLASMOPY_COMMIT}', \
      '/tmp/plasmopy.tar.gz')" \
 && echo "${PLASMOPY_SHA256}  /tmp/plasmopy.tar.gz" | sha256sum -c - \
 && mkdir /opt/plasmopy \
 && tar xzf /tmp/plasmopy.tar.gz -C /opt/plasmopy --strip-components=1 \
 && rm /tmp/plasmopy.tar.gz \
 && chmod -R a+rX /opt/plasmopy

COPY plasmopy/generate.py /oracles/plasmopy/generate.py
WORKDIR /oracles
