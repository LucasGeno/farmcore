# Optional oracle container: installs copyleft reference implementations at build time and runs
# FarmCore's own generator scripts against them. Nothing here is packaged, nothing runs in CI,
# and no copyleft source is copied into the repository. Build and run with tools/oracles/run.sh.
#
# rocker/r-ver pins CRAN to a dated Posit Package Manager snapshot for its R version.
FROM rocker/r-ver:4.4.2

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
