"""Weather providers behind one contract, writing one normalised schema (core_0008).

`contract` is what a provider implements, `registry` holds the installed providers, `store`
writes their receipts, stations, observations and forecasts, and `variables` names the CF
variables FarmCore stores. Apps adapt their existing sources; the core imports no app.
"""
