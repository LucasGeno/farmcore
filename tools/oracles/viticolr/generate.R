# FarmCore oracle generator for viticolR (Rossi et al. 2008 downy mildew primary infections).
#
# This script is FarmCore's own (Apache-2.0). It calls the GPL viticolR package (and its MIT
# dependency epiphytoolR), installed only inside the oracle container, on the synthetic hourly
# season and writes the primary-infection stage times as one JSON fixture.
# Usage (inside the container): Rscript viticolr/generate.R <season csv> <output dir>

suppressPackageStartupMessages({
  library(viticolR)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
season_csv <- args[1]
out_dir <- args[2]
if (is.na(out_dir)) stop("usage: Rscript viticolr/generate.R <season csv> <output dir>")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

raw <- read.csv(season_csv, stringsAsFactors = FALSE)
raw$times <- as.POSIXct(raw$time_utc, format = "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
# viticolR reads temperature, humidity and rain. format_weather() also requires wind and a
# station; these constants do not enter the Rossi equations as viticolR implements them.
raw$ws <- 0
raw$wd <- 0
raw$wd_sd <- 0
raw$station <- "farmcore-synthetic"
raw$lon <- 7.0
raw$lat <- 46.0

w <- epiphytoolR::format_weather(
  raw,
  POSIXct_time = "times",
  time_zone = "UTC",
  temp = "temp_c",
  rain = "rain_mm",
  rh = "rh_pct",
  ws = "ws",
  wd = "wd",
  wd_sd = "wd_sd",
  station = "station",
  lon = "lon",
  lat = "lat",
  verbose = FALSE
)

# Explicit season: viticolR's "detect" assumes a southern-hemisphere season and reads the
# clock. Northern hemisphere: every oospore is morphologically mature on 1 January (SOD = 1).
start <- "2025-01-01 00:00:00"
end <- "2025-07-31 23:00:00"
sod <- 1
mod <- estimate_DM_PI(w, Start = start, End = end, SOD = sod)
pi <- get_PI_dates(mod)
pi$primary_infection_stage <- as.character(pi$primary_infection_stage)
pi$hour <- ifelse(is.na(pi$hour), NA, format(pi$hour, "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"))
pi <- pi[order(pi$cohort, match(pi$primary_infection_stage, unique(pi$primary_infection_stage)))]

daily <- data.frame(
  date = format(mod$time_hours, "%Y-%m-%d", tz = "UTC"),
  Hyd_t = mod$Hyd_t,
  PMO = mod$PMO
)
daily <- daily[!duplicated(daily$date, fromLast = TRUE), ]

desc <- packageDescription("viticolR")
fixture <- list(
  format = "farmcore-oracle-fixture/1",
  generator = list(
    name = "viticolR",
    version = sprintf("%s (commit %s)", desc$Version, Sys.getenv("VITICOLR_COMMIT")),
    licence = desc$License,
    source = "https://github.com/PaulMelloy/viticolR",
    source_sha256 = Sys.getenv("VITICOLR_SHA256"),
    authors = "Paul Melloy",
    dependencies = list(list(
      name = "epiphytoolR",
      version = sprintf("%s (commit %s)", packageDescription("epiphytoolR")$Version,
                        Sys.getenv("EPIPHYTOOLR_COMMIT")),
      licence = packageDescription("epiphytoolR")$License,
      source_sha256 = Sys.getenv("EPIPHYTOOLR_SHA256")
    )),
    runtime = R.version.string,
    run = "out of process, in the tools/oracles container"
  ),
  method_reference = paste(
    "Rossi, V., Caffi, T., Giosue, S. & Bugiani, R. (2008). A mechanistic model simulating",
    "primary infections of downy mildew in grapevine. Ecological Modelling 212, 480-491.",
    "viticolR describes itself as translated and adapted from this paper."
  ),
  call = "viticolR::estimate_DM_PI then viticolR::get_PI_dates",
  parameters = list(
    Start = start,
    End = end,
    SOD = sod,
    station = list(lat = 46.0, lon = 7.0, ws = 0, wd = 0, wd_sd = 0)
  ),
  inputs = list(
    file = paste0("inputs/", basename(season_csv)),
    sha256 = strsplit(system2("sha256sum", season_csv, stdout = TRUE), " ")[[1]][1],
    units = list(temp_c = "degC", rh_pct = "%", rain_mm = "mm", leaf_wet_min = "min (unused)")
  ),
  outputs = list(
    cohorts = mod$cohorts,
    primary_infection_stages = pi,
    end_of_day = daily
  )
)
path <- file.path(out_dir, "season-2025.json")
write_json(fixture, path, auto_unbox = TRUE, digits = NA, pretty = TRUE, null = "null",
           na = "null", dataframe = "rows")
cat("wrote", path, "cohorts:", mod$cohorts, "\n")
