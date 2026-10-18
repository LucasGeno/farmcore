"""App `share`: the farmer grants a chosen third party read scopes, sees every read, revokes.

Framing (R9): the third party the farmer chooses under the Data Act (Art. 5). Grantee reads go only
through a `ScopedRepository` bound to the grant (`service.grant_repo`); done sprays export as
FIWARE `AgriParcelOperation` NGSI-LD (`ngsi.py`).
"""
