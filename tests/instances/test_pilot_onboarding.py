"""Two independently migrated pilot installations, provisioned only by CLI and HTTP."""

import json
import secrets
from contextlib import ExitStack

from fastapi.testclient import TestClient

from farmcore.main import create_app
from farmcore.settings import Settings
from tests.apps.fields.test_parcel_import import feature
from tests.instances.support import run_cli
from tests.instances.test_isolation import install  # noqa: F401


def test_two_private_pilots_first_login_and_parcels(install, tmp_path):  # noqa: F811
    with ExitStack() as stack:
        browsers = []
        ids = []
        for index in range(2):
            inst = install()
            env = {"DATABASE_URL": inst.runtime_url}
            farm = run_cli("farms", "create", f"Synthetic pilot {index}", env=env)
            assert farm.exit_code == 0, farm.output
            farm_id = farm.output.split()[1]
            foreign = run_cli("farms", "create", "Unassigned synthetic farm", env=env)
            assert foreign.exit_code == 0
            foreign_id = foreign.output.split()[1]
            password = secrets.token_urlsafe(20)
            result = run_cli(
                "users",
                "create",
                "grower",
                "--farm-id",
                farm_id,
                "--password-stdin",
                env=env,
                input=password + "\n",
            )
            assert result.exit_code == 0, result.output
            parcel = feature(f"P{index}")
            path = tmp_path / f"pilot{index}.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [parcel]}))
            result = run_cli("parcels", "import", str(path), "--farm-id", farm_id, env=env)
            assert result.exit_code == 0, result.output
            browser = stack.enter_context(
                TestClient(
                    create_app(Settings(_env_file=None, database_url=inst.runtime_url)),
                    base_url="https://pilot.test",
                )
            )
            assert browser.get(f"/fields/parcels.geojson?farm={farm_id}").status_code in (401, 403)
            health = browser.get("/health").json()
            ids.append(health["installation_id"])
            page = browser.get("/login")
            csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
            login = browser.post(
                "/login",
                data={"login": "grower", "password": password, "csrf_token": csrf},
                headers={"Origin": "https://pilot.test"},
                follow_redirects=False,
            )
            assert login.status_code == 303, login.text
            features = browser.get(f"/fields/parcels.geojson?farm={farm_id}").json()["features"]
            assert [f["properties"]["alias"] for f in features] == [f"P{index}"]
            assert browser.get(f"/fields/parcels.geojson?farm={foreign_id}").status_code in (
                403,
                404,
            )
            browsers.append(browser)
        assert ids[0] != ids[1]
        # Same browser host and overlapping numeric farm IDs must not confer authority.
        own = dict(browsers[1].cookies)
        browsers[1].cookies.clear()
        browsers[1].cookies.update(browsers[0].cookies)
        assert browsers[1].get("/fields/parcels.geojson?farm=1").status_code in (401, 403)
        browsers[1].cookies.clear()
        browsers[1].cookies.update(own)
        assert browsers[1].get("/fields/parcels.geojson?farm=1").status_code == 200
