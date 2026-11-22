# FarmCore oracle generator for degday (Zalom et al. 1983 degree-day methods).
#
# This script is FarmCore's own (Apache-2.0). It calls the GPL degday package, which is
# installed only inside the oracle container, and writes one JSON fixture per method and cutoff.
# Usage (inside the container): Rscript degday/generate.R <output dir>

suppressPackageStartupMessages({
  library(degday)
  library(jsonlite)
})

out_dir <- commandArgs(trailingOnly = TRUE)[1]
if (is.na(out_dir)) stop("usage: Rscript degday/generate.R <output dir>")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

# One day per Zalom et al. (1983) case against a 10/30 degC window, plus edge days.
# The thresholds are harness case parameters, not agronomic claims.
days <- data.frame(
  case = c(
    "entirely below lower threshold",
    "crosses lower threshold only",
    "between thresholds",
    "crosses upper threshold only",
    "crosses both thresholds",
    "entirely above upper threshold",
    "min and max exactly on the thresholds",
    "flat day (min equals max)",
    "last day (next-day min is its own min)"
  ),
  tmin = c(2, 5, 12, 15, 8, 31, 10, 20, 14),
  tmax = c(8, 18, 24, 34, 36, 38, 30, 20, 28)
)
# Double methods model the second half-day from the next day's minimum.
days$nextday_min <- c(days$tmin[-1], days$tmin[nrow(days)])

thresh_low <- 10
thresh_up <- 30
digits <- 6

methods <- c("sng_sine", "dbl_sine", "sng_tri", "dbl_tri")
runs <- list()
for (m in methods) {
  for (cutoff in c("horizontal", "vertical", "intermediate")) {
    runs[[length(runs) + 1]] <- list(method = m, cutoff = cutoff, thresh_up = thresh_up)
  }
  # degday needs an upper threshold for these methods; its error message says to pass a large
  # number such as 200 when there is none. This run is that "no upper threshold" case.
  runs[[length(runs) + 1]] <- list(method = m, cutoff = "horizontal", thresh_up = 200)
}

desc <- packageDescription("degday")
generator <- list(
  name = "degday",
  version = as.character(packageVersion("degday")),
  licence = desc$License,
  source = "https://cran.r-project.org/package=degday",
  source_sha256 = Sys.getenv("DEGDAY_SHA256"),
  authors = "Andy Lyons; Regents of the University of California",
  runtime = R.version.string,
  run = "out of process, in the tools/oracles container"
)

for (r in runs) {
  args <- list(
    daily_min = days$tmin,
    daily_max = days$tmax,
    nextday_min = days$nextday_min,
    thresh_low = thresh_low,
    thresh_up = r$thresh_up,
    method = r$method,
    cutoff = r$cutoff,
    digits = digits,
    cumulative = FALSE,
    no_neg = TRUE,
    quiet = TRUE
  )
  upper <- if (r$thresh_up == 200) "upper_200" else r$cutoff
  # degday refuses some method/cutoff pairs; report them rather than fabricate output.
  dd <- tryCatch(do.call(dd_calc, args), error = function(e) {
    cat("unsupported by degday:", r$method, upper, "-", conditionMessage(e), "\n")
    NULL
  })
  if (is.null(dd)) next
  fixture <- list(
    format = "farmcore-oracle-fixture/1",
    generator = generator,
    method_reference = paste(
      "Zalom, F.G., Goodell, P.B., Wilson, L.T., Barnett, W.W. & Bentley, W.J. (1983).",
      "Degree-days: the calculation and use of heat units in pest management.",
      "UC DANR Leaflet 21373."
    ),
    call = "degday::dd_calc",
    parameters = list(
      method = r$method,
      cutoff = r$cutoff,
      thresh_low = thresh_low,
      thresh_up = r$thresh_up,
      digits = digits,
      cumulative = FALSE,
      no_neg = TRUE
    ),
    inputs = list(
      units = "degC",
      case = days$case,
      daily_min = days$tmin,
      daily_max = days$tmax,
      nextday_min = days$nextday_min
    ),
    outputs = list(units = "degC day", daily_degree_days = dd)
  )
  path <- file.path(out_dir, sprintf("%s-%s.json", r$method, upper))
  write_json(fixture, path, auto_unbox = TRUE, digits = NA, pretty = TRUE, null = "null")
  cat("wrote", path, "\n")
}
