# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Unit tests for Superset"""
import json
from io import BytesIO
from unittest import mock
from zipfile import is_zipfile, ZipFile

import prison
import pytest
import yaml
from flask_babel import lazy_gettext as _
from parameterized import parameterized
from sqlalchemy import and_
from sqlalchemy.sql import func

from superset.commands.chart.data.get_data_command import ChartDataCommand
from superset.commands.chart.exceptions import ChartDataQueryFailedError
from superset.connectors.sqla.models import SqlaTable
from superset.extensions import cache_manager, db, security_manager
from superset.models.core import Database, FavStar, FavStarClassName
from superset.models.dashboard import Dashboard
from superset.models.slice import Slice
from superset.reports.models import ReportSchedule, ReportScheduleType
from superset.utils.core import get_example_default_schema
from superset.utils.database import get_example_database
from superset.viz import viz_types
from tests.integration_tests.base_api_tests import ApiOwnersTestCaseMixin
from tests.integration_tests.base_tests import SupersetTestCase
from tests.integration_tests.conftest import with_feature_flags
from tests.integration_tests.fixtures.birth_names_dashboard import (
    load_birth_names_dashboard_with_slices,
    load_birth_names_data,
)
from tests.integration_tests.fixtures.energy_dashboard import (
    load_energy_table_data,
    load_energy_table_with_slice,
)
from tests.integration_tests.fixtures.importexport import (
    chart_config,
    chart_metadata_config,
    database_config,
    dataset_config,
    dataset_metadata_config,
)
from tests.integration_tests.fixtures.unicode_dashboard import (
    load_unicode_dashboard_with_slice,
    load_unicode_data,
)
from tests.integration_tests.fixtures.world_bank_dashboard import (
    load_world_bank_dashboard_with_slices,
    load_world_bank_data,
)
from tests.integration_tests.insert_chart_mixin import InsertChartMixin
from tests.integration_tests.test_app import app
from tests.integration_tests.utils.get_dashboards import get_dashboards_ids

CHART_DATA_URI = "api/v1/chart/data"
CHARTS_FIXTURE_COUNT = 10


class TestChartApi(SupersetTestCase, ApiOwnersTestCaseMixin, InsertChartMixin):
    resource_name = "chart"

    @pytest.fixture(autouse=True)
    def clear_data_cache(self):
        with app.app_context():
            cache_manager.data_cache.clear()
            yield

    @pytest.fixture()
    def create_charts(self):
        with self.create_app().app_context():
            charts = []
            admin = self.get_user("admin")
            for cx in range(CHARTS_FIXTURE_COUNT - 1):
                charts.append(self.insert_chart(f"name{cx}", [admin.id], 1))
            fav_charts = []
            for cx in range(round(CHARTS_FIXTURE_COUNT / 2)):
                fav_star = FavStar(
                    user_id=admin.id, class_name="slice", obj_id=charts[cx].id
                )
                db.session.add(fav_star)
                db.session.commit()
                fav_charts.append(fav_star)
            yield charts

            # rollback changes
            for chart in charts:
                db.session.delete(chart)
            for fav_chart in fav_charts:
                db.session.delete(fav_chart)
            db.session.commit()

    @pytest.fixture()
    def create_charts_created_by_gamma(self):
        with self.create_app().app_context():
            charts = []
            user = self.get_user("gamma")
            for cx in range(CHARTS_FIXTURE_COUNT - 1):
                charts.append(self.insert_chart(f"gamma{cx}", [user.id], 1))
            yield charts
            # rollback changes
            for chart in charts:
                db.session.delete(chart)
            db.session.commit()

    @pytest.fixture()
    def create_certified_charts(self):
        with self.create_app().app_context():
            certified_charts = []
            admin = self.get_user("admin")
            for cx in range(CHARTS_FIXTURE_COUNT):
                certified_charts.append(
                    self.insert_chart(
                        f"certified{cx}",
                        [admin.id],
                        1,
                        certified_by="John Doe",
                        certification_details="Sample certification",
                    )
                )

            yield certified_charts

            # rollback changes
            for chart in certified_charts:
                db.session.delete(chart)
            db.session.commit()

    @pytest.fixture()
    def create_chart_with_report(self):
        with self.create_app().app_context():
            admin = self.get_user("admin")
            chart = self.insert_chart(f"chart_report", [admin.id], 1)
            report_schedule = ReportSchedule(
                type=ReportScheduleType.REPORT,
                name="report_with_chart",
                crontab="* * * * *",
                chart=chart,
            )
            db.session.commit()

            yield chart

            # rollback changes
            db.session.delete(report_schedule)
            db.session.delete(chart)
            db.session.commit()

    @pytest.fixture()
    def add_dashboard_to_chart(self):
        with self.create_app().app_context():
            admin = self.get_user("admin")

            self.chart = self.insert_chart("My chart", [admin.id], 1)

            self.original_dashboard = Dashboard()
            self.original_dashboard.dashboard_title = "Original Dashboard"
            self.original_dashboard.slug = "slug"
            self.original_dashboard.owners = [admin]
            self.original_dashboard.slices = [self.chart]
            self.original_dashboard.published = False
            db.session.add(self.original_dashboard)

            self.new_dashboard = Dashboard()
            self.new_dashboard.dashboard_title = "New Dashboard"
            self.new_dashboard.slug = "new_slug"
            self.new_dashboard.owners = [admin]
            self.new_dashboard.published = False
            db.session.add(self.new_dashboard)

            db.session.commit()

            yield self.chart

            db.session.delete(self.original_dashboard)
            db.session.delete(self.new_dashboard)
            db.session.delete(self.chart)
            db.session.commit()

    def test_info_security_chart(self):
        """
        Chart API: Test info security
        """
        self.login(username="admin")
        params = {"keys": ["permissions"]}
        uri = f"api/v1/chart/_info?q={prison.dumps(params)}"
        rv = self.get_assert_metric(uri, "info")
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert set(data["permissions"]) == {
            "can_read",
            "can_write",
            "can_export",
            "can_warm_up_cache",
        }

    def create_chart_import(self):
        buf = BytesIO()
        with ZipFile(buf, "w") as bundle:
            with bundle.open("chart_export/metadata.yaml", "w") as fp:
                fp.write(yaml.safe_dump(chart_metadata_config).encode())
            with bundle.open(
                "chart_export/databases/imported_database.yaml", "w"
            ) as fp:
                fp.write(yaml.safe_dump(database_config).encode())
            with bundle.open("chart_export/datasets/imported_dataset.yaml", "w") as fp:
                fp.write(yaml.safe_dump(dataset_config).encode())
            with bundle.open("chart_export/charts/imported_chart.yaml", "w") as fp:
                fp.write(yaml.safe_dump(chart_config).encode())
        buf.seek(0)
        return buf

    def test_delete_chart(self):
        """
        Chart API: Test delete
        """
        admin_id = self.get_user("admin").id
        chart_id = self.insert_chart("name", [admin_id], 1).id
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.delete_assert_metric(uri, "delete")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)
        self.assertEqual(model, None)

    def test_delete_bulk_charts(self):
        """
        Chart API: Test delete bulk
        """
        admin = self.get_user("admin")
        chart_count = 4
        chart_ids = list()
        for chart_name_index in range(chart_count):
            chart_ids.append(
                self.insert_chart(f"title{chart_name_index}", [admin.id], 1, admin).id
            )
        self.login(username="admin")
        argument = chart_ids
        uri = f"api/v1/chart/?q={prison.dumps(argument)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        self.assertEqual(rv.status_code, 200)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": f"Deleted {chart_count} charts"}
        self.assertEqual(response, expected_response)
        for chart_id in chart_ids:
            model = db.session.query(Slice).get(chart_id)
            self.assertEqual(model, None)

    def test_delete_bulk_chart_bad_request(self):
        """
        Chart API: Test delete bulk bad request
        """
        chart_ids = [1, "a"]
        self.login(username="admin")
        argument = chart_ids
        uri = f"api/v1/chart/?q={prison.dumps(argument)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        self.assertEqual(rv.status_code, 400)

    def test_delete_not_found_chart(self):
        """
        Chart API: Test not found delete
        """
        self.login(username="admin")
        chart_id = 1000
        uri = f"api/v1/chart/{chart_id}"
        rv = self.delete_assert_metric(uri, "delete")
        self.assertEqual(rv.status_code, 404)

    @pytest.mark.usefixtures("create_chart_with_report")
    def test_delete_chart_with_report(self):
        """
        Chart API: Test delete with associated report
        """
        self.login(username="admin")
        chart = (
            db.session.query(Slice)
            .filter(Slice.slice_name == "chart_report")
            .one_or_none()
        )
        uri = f"api/v1/chart/{chart.id}"
        rv = self.client.delete(uri)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(rv.status_code, 422)
        expected_response = {
            "message": "There are associated alerts or reports: report_with_chart"
        }
        self.assertEqual(response, expected_response)

    def test_delete_bulk_charts_not_found(self):
        """
        Chart API: Test delete bulk not found
        """
        max_id = db.session.query(func.max(Slice.id)).scalar()
        chart_ids = [max_id + 1, max_id + 2]
        self.login(username="admin")
        uri = f"api/v1/chart/?q={prison.dumps(chart_ids)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        self.assertEqual(rv.status_code, 404)

    @pytest.mark.usefixtures("create_chart_with_report", "create_charts")
    def test_bulk_delete_chart_with_report(self):
        """
        Chart API: Test bulk delete with associated report
        """
        self.login(username="admin")
        chart_with_report = (
            db.session.query(Slice.id)
            .filter(Slice.slice_name == "chart_report")
            .one_or_none()
        )

        charts = db.session.query(Slice.id).filter(Slice.slice_name.like("name%")).all()
        chart_ids = [chart.id for chart in charts]
        chart_ids.append(chart_with_report.id)

        uri = f"api/v1/chart/?q={prison.dumps(chart_ids)}"
        rv = self.client.delete(uri)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(rv.status_code, 422)
        expected_response = {
            "message": "There are associated alerts or reports: report_with_chart"
        }
        self.assertEqual(response, expected_response)

    def test_delete_chart_admin_not_owned(self):
        """
        Chart API: Test admin delete not owned
        """
        gamma_id = self.get_user("gamma").id
        chart_id = self.insert_chart("title", [gamma_id], 1).id

        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.delete_assert_metric(uri, "delete")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)
        self.assertEqual(model, None)

    def test_delete_bulk_chart_admin_not_owned(self):
        """
        Chart API: Test admin delete bulk not owned
        """
        gamma_id = self.get_user("gamma").id
        chart_count = 4
        chart_ids = list()
        for chart_name_index in range(chart_count):
            chart_ids.append(
                self.insert_chart(f"title{chart_name_index}", [gamma_id], 1).id
            )

        self.login(username="admin")
        argument = chart_ids
        uri = f"api/v1/chart/?q={prison.dumps(argument)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(rv.status_code, 200)
        expected_response = {"message": f"Deleted {chart_count} charts"}
        self.assertEqual(response, expected_response)

        for chart_id in chart_ids:
            model = db.session.query(Slice).get(chart_id)
            self.assertEqual(model, None)

    def test_delete_chart_not_owned(self):
        """
        Chart API: Test delete try not owned
        """
        user_alpha1 = self.create_user(
            "alpha1", "password", "Alpha", email="alpha1@superset.org"
        )
        user_alpha2 = self.create_user(
            "alpha2", "password", "Alpha", email="alpha2@superset.org"
        )
        chart = self.insert_chart("title", [user_alpha1.id], 1)
        self.login(username="alpha2", password="password")
        uri = f"api/v1/chart/{chart.id}"
        rv = self.delete_assert_metric(uri, "delete")
        self.assertEqual(rv.status_code, 403)
        db.session.delete(chart)
        db.session.delete(user_alpha1)
        db.session.delete(user_alpha2)
        db.session.commit()

    def test_delete_bulk_chart_not_owned(self):
        """
        Chart API: Test delete bulk try not owned
        """
        user_alpha1 = self.create_user(
            "alpha1", "password", "Alpha", email="alpha1@superset.org"
        )
        user_alpha2 = self.create_user(
            "alpha2", "password", "Alpha", email="alpha2@superset.org"
        )

        chart_count = 4
        charts = list()
        for chart_name_index in range(chart_count):
            charts.append(
                self.insert_chart(f"title{chart_name_index}", [user_alpha1.id], 1)
            )

        owned_chart = self.insert_chart("title_owned", [user_alpha2.id], 1)

        self.login(username="alpha2", password="password")

        # verify we can't delete not owned charts
        arguments = [chart.id for chart in charts]
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        self.assertEqual(rv.status_code, 403)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": "Forbidden"}
        self.assertEqual(response, expected_response)

        # # nothing is deleted in bulk with a list of owned and not owned charts
        arguments = [chart.id for chart in charts] + [owned_chart.id]
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        self.assertEqual(rv.status_code, 403)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": "Forbidden"}
        self.assertEqual(response, expected_response)

        for chart in charts:
            db.session.delete(chart)
        db.session.delete(owned_chart)
        db.session.delete(user_alpha1)
        db.session.delete(user_alpha2)
        db.session.commit()

    @pytest.mark.usefixtures(
        "load_world_bank_dashboard_with_slices",
        "load_birth_names_dashboard_with_slices",
    )
    def test_create_chart(self):
        """
        Chart API: Test create chart
        """
        dashboards_ids = get_dashboards_ids(db, ["world_health", "births"])
        admin_id = self.get_user("admin").id
        chart_data = {
            "slice_name": "name1",
            "description": "description1",
            "owners": [admin_id],
            "viz_type": "viz_type1",
            "params": "1234",
            "cache_timeout": 1000,
            "datasource_id": 1,
            "datasource_type": "table",
            "dashboards": dashboards_ids,
            "certified_by": "John Doe",
            "certification_details": "Sample certification",
        }
        self.login(username="admin")
        uri = "api/v1/chart/"
        rv = self.post_assert_metric(uri, chart_data, "post")
        self.assertEqual(rv.status_code, 201)
        data = json.loads(rv.data.decode("utf-8"))
        model = db.session.query(Slice).get(data.get("id"))
        db.session.delete(model)
        db.session.commit()

    def test_create_simple_chart(self):
        """
        Chart API: Test create simple chart
        """
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "table",
        }
        self.login(username="admin")
        uri = "api/v1/chart/"
        rv = self.post_assert_metric(uri, chart_data, "post")
        self.assertEqual(rv.status_code, 201)
        data = json.loads(rv.data.decode("utf-8"))
        model = db.session.query(Slice).get(data.get("id"))
        db.session.delete(model)
        db.session.commit()

    def test_create_chart_validate_owners(self):
        """
        Chart API: Test create validate owners
        """
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "table",
            "owners": [1000],
        }
        self.login(username="admin")
        uri = "api/v1/chart/"
        rv = self.post_assert_metric(uri, chart_data, "post")
        self.assertEqual(rv.status_code, 422)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": {"owners": ["Owners are invalid"]}}
        self.assertEqual(response, expected_response)

    def test_create_chart_validate_params(self):
        """
        Chart API: Test create validate params json
        """
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "table",
            "params": '{"A:"a"}',
        }
        self.login(username="admin")
        uri = "api/v1/chart/"
        rv = self.post_assert_metric(uri, chart_data, "post")
        self.assertEqual(rv.status_code, 400)

    def test_create_chart_validate_datasource(self):
        """
        Chart API: Test create validate datasource
        """
        self.login(username="admin")
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "unknown",
        }
        rv = self.post_assert_metric("/api/v1/chart/", chart_data, "post")
        self.assertEqual(rv.status_code, 400)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            response,
            {
                "message": {
                    "datasource_type": [
                        "Must be one of: sl_table, table, dataset, query, saved_query, view."
                    ]
                }
            },
        )
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 0,
            "datasource_type": "table",
        }
        rv = self.post_assert_metric("/api/v1/chart/", chart_data, "post")
        self.assertEqual(rv.status_code, 422)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            response, {"message": {"datasource_id": ["Datasource does not exist"]}}
        )

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_create_chart_validate_user_is_dashboard_owner(self):
        """
        Chart API: Test create validate user is dashboard owner
        """
        dash = db.session.query(Dashboard).filter_by(slug="world_health").first()
        # Must be published so that alpha user has read access to dash
        dash.published = True
        db.session.commit()
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "table",
            "dashboards": [dash.id],
        }
        self.login(username="alpha")
        uri = "api/v1/chart/"
        rv = self.post_assert_metric(uri, chart_data, "post")
        self.assertEqual(rv.status_code, 403)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            response,
            {"message": "Changing one or more of these dashboards is forbidden"},
        )

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_update_chart(self):
        """
        Chart API: Test update
        """
        schema = get_example_default_schema()
        full_table_name = f"{schema}.birth_names" if schema else "birth_names"

        admin = self.get_user("admin")
        gamma = self.get_user("gamma")
        birth_names_table_id = SupersetTestCase.get_table(name="birth_names").id
        chart_id = self.insert_chart(
            "title", [admin.id], birth_names_table_id, admin
        ).id
        dash_id = db.session.query(Dashboard.id).filter_by(slug="births").first()[0]
        chart_data = {
            "slice_name": "title1_changed",
            "description": "description1",
            "owners": [gamma.id],
            "viz_type": "viz_type1",
            "params": """{"a": 1}""",
            "cache_timeout": 1000,
            "datasource_id": birth_names_table_id,
            "datasource_type": "table",
            "dashboards": [dash_id],
            "certified_by": "Mario Rossi",
            "certification_details": "Edited certification",
        }
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)
        related_dashboard = db.session.query(Dashboard).filter_by(slug="births").first()
        self.assertEqual(model.created_by, admin)
        self.assertEqual(model.slice_name, "title1_changed")
        self.assertEqual(model.description, "description1")
        self.assertNotIn(admin, model.owners)
        self.assertIn(gamma, model.owners)
        self.assertEqual(model.viz_type, "viz_type1")
        self.assertEqual(model.params, """{"a": 1}""")
        self.assertEqual(model.cache_timeout, 1000)
        self.assertEqual(model.datasource_id, birth_names_table_id)
        self.assertEqual(model.datasource_type, "table")
        self.assertEqual(model.datasource_name, full_table_name)
        self.assertEqual(model.certified_by, "Mario Rossi")
        self.assertEqual(model.certification_details, "Edited certification")
        self.assertIn(model.id, [slice.id for slice in related_dashboard.slices])
        db.session.delete(model)
        db.session.commit()

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_chart_get_list_no_username(self):
        """
        Chart API: Tests that no username is returned
        """
        admin = self.get_user("admin")
        birth_names_table_id = SupersetTestCase.get_table(name="birth_names").id
        chart_id = self.insert_chart("title", [admin.id], birth_names_table_id).id
        chart_data = {
            "slice_name": (new_name := "title1_changed"),
            "owners": [admin.id],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)

        response = self.get_assert_metric("api/v1/chart/", "get_list")
        res = json.loads(response.data.decode("utf-8"))["result"]

        current_chart = [d for d in res if d["id"] == chart_id][0]
        self.assertEqual(current_chart["slice_name"], new_name)
        self.assertNotIn("username", current_chart["changed_by"].keys())
        self.assertNotIn("username", current_chart["owners"][0].keys())

        db.session.delete(model)
        db.session.commit()

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_chart_get_no_username(self):
        """
        Chart API: Tests that no username is returned
        """
        admin = self.get_user("admin")
        birth_names_table_id = SupersetTestCase.get_table(name="birth_names").id
        chart_id = self.insert_chart("title", [admin.id], birth_names_table_id).id
        chart_data = {
            "slice_name": (new_name := "title1_changed"),
            "owners": [admin.id],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)

        response = self.get_assert_metric(uri, "get")
        res = json.loads(response.data.decode("utf-8"))["result"]

        self.assertEqual(res["slice_name"], new_name)
        self.assertNotIn("username", res["owners"][0].keys())

        db.session.delete(model)
        db.session.commit()

    def test_update_chart_new_owner_not_admin(self):
        """
        Chart API: Test update set new owner implicitly adds logged in owner
        """
        gamma = self.get_user("gamma_no_csv")
        alpha = self.get_user("alpha")
        chart_id = self.insert_chart("title", [gamma.id], 1).id
        chart_data = {
            "slice_name": (new_name := "title1_changed"),
            "owners": [alpha.id],
        }
        self.login(username=gamma.username)
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        assert rv.status_code == 200
        model = db.session.query(Slice).get(chart_id)
        assert model.slice_name == new_name
        assert alpha in model.owners
        assert gamma in model.owners
        db.session.delete(model)
        db.session.commit()

    def test_update_chart_new_owner_admin(self):
        """
        Chart API: Test update set new owner as admin to other than current user
        """
        gamma = self.get_user("gamma")
        admin = self.get_user("admin")
        chart_id = self.insert_chart("title", [admin.id], 1).id
        chart_data = {"slice_name": "title1_changed", "owners": [gamma.id]}
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        model = db.session.query(Slice).get(chart_id)
        self.assertNotIn(admin, model.owners)
        self.assertIn(gamma, model.owners)
        db.session.delete(model)
        db.session.commit()

    @pytest.mark.usefixtures("add_dashboard_to_chart")
    def test_update_chart_preserve_ownership(self):
        """
        Chart API: Test update chart preserves owner list (if un-changed)
        """
        chart_data = {
            "slice_name": "title1_changed",
        }
        admin = self.get_user("admin")
        self.login(username="admin")
        uri = f"api/v1/chart/{self.chart.id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual([admin], self.chart.owners)

    @pytest.mark.usefixtures("add_dashboard_to_chart")
    def test_update_chart_clear_owner_list(self):
        """
        Chart API: Test update chart admin can clear owner list
        """
        chart_data = {"slice_name": "title1_changed", "owners": []}
        admin = self.get_user("admin")
        self.login(username="admin")
        uri = f"api/v1/chart/{self.chart.id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual([], self.chart.owners)

    def test_update_chart_populate_owner(self):
        """
        Chart API: Test update admin can update chart with
        no owners to a different owner
        """
        gamma = self.get_user("gamma")
        admin = self.get_user("admin")
        chart_id = self.insert_chart("title", [], 1).id
        model = db.session.query(Slice).get(chart_id)
        self.assertEqual(model.owners, [])
        chart_data = {"owners": [gamma.id]}
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        model_updated = db.session.query(Slice).get(chart_id)
        self.assertNotIn(admin, model_updated.owners)
        self.assertIn(gamma, model_updated.owners)
        db.session.delete(model_updated)
        db.session.commit()

    @pytest.mark.usefixtures("add_dashboard_to_chart")
    def test_update_chart_new_dashboards(self):
        """
        Chart API: Test update chart associating it with new dashboard
        """
        chart_data = {
            "slice_name": "title1_changed",
            "dashboards": [self.new_dashboard.id],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/{self.chart.id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        self.assertIn(self.new_dashboard, self.chart.dashboards)
        self.assertNotIn(self.original_dashboard, self.chart.dashboards)

    @pytest.mark.usefixtures("add_dashboard_to_chart")
    def test_not_update_chart_none_dashboards(self):
        """
        Chart API: Test update chart without changing dashboards configuration
        """
        chart_data = {"slice_name": "title1_changed_again"}
        self.login(username="admin")
        uri = f"api/v1/chart/{self.chart.id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)
        self.assertIn(self.original_dashboard, self.chart.dashboards)
        self.assertEqual(len(self.chart.dashboards), 1)

    def test_update_chart_not_owned(self):
        """
        Chart API: Test update not owned
        """
        user_alpha1 = self.create_user(
            "alpha1", "password", "Alpha", email="alpha1@superset.org"
        )
        user_alpha2 = self.create_user(
            "alpha2", "password", "Alpha", email="alpha2@superset.org"
        )
        chart = self.insert_chart("title", [user_alpha1.id], 1)

        self.login(username="alpha2", password="password")
        chart_data = {"slice_name": "title1_changed"}
        uri = f"api/v1/chart/{chart.id}"
        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 403)
        db.session.delete(chart)
        db.session.delete(user_alpha1)
        db.session.delete(user_alpha2)
        db.session.commit()

    def test_update_chart_linked_with_not_owned_dashboard(self):
        """
        Chart API: Test update chart which is linked to not owned dashboard
        """
        user_alpha1 = self.create_user(
            "alpha1", "password", "Alpha", email="alpha1@superset.org"
        )
        user_alpha2 = self.create_user(
            "alpha2", "password", "Alpha", email="alpha2@superset.org"
        )
        chart = self.insert_chart("title", [user_alpha1.id], 1)

        original_dashboard = Dashboard()
        original_dashboard.dashboard_title = "Original Dashboard"
        original_dashboard.slug = "slug"
        original_dashboard.owners = [user_alpha1]
        original_dashboard.slices = [chart]
        original_dashboard.published = False
        db.session.add(original_dashboard)

        new_dashboard = Dashboard()
        new_dashboard.dashboard_title = "Cloned Dashboard"
        new_dashboard.slug = "new_slug"
        new_dashboard.owners = [user_alpha2]
        new_dashboard.slices = [chart]
        new_dashboard.published = False
        db.session.add(new_dashboard)

        self.login(username="alpha1", password="password")
        chart_data_with_invalid_dashboard = {
            "slice_name": "title1_changed",
            "dashboards": [original_dashboard.id, 0],
        }
        chart_data = {
            "slice_name": "title1_changed",
            "dashboards": [original_dashboard.id, new_dashboard.id],
        }
        uri = f"api/v1/chart/{chart.id}"

        rv = self.put_assert_metric(uri, chart_data_with_invalid_dashboard, "put")
        self.assertEqual(rv.status_code, 422)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": {"dashboards": ["Dashboards do not exist"]}}
        self.assertEqual(response, expected_response)

        rv = self.put_assert_metric(uri, chart_data, "put")
        self.assertEqual(rv.status_code, 200)

        db.session.delete(chart)
        db.session.delete(original_dashboard)
        db.session.delete(new_dashboard)
        db.session.delete(user_alpha1)
        db.session.delete(user_alpha2)
        db.session.commit()

    def test_update_chart_validate_datasource(self):
        """
        Chart API: Test update validate datasource
        """
        admin = self.get_user("admin")
        chart = self.insert_chart("title", owners=[admin.id], datasource_id=1)
        self.login(username="admin")

        chart_data = {"datasource_id": 1, "datasource_type": "unknown"}
        rv = self.put_assert_metric(f"/api/v1/chart/{chart.id}", chart_data, "put")
        self.assertEqual(rv.status_code, 400)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            response,
            {
                "message": {
                    "datasource_type": [
                        "Must be one of: sl_table, table, dataset, query, saved_query, view."
                    ]
                }
            },
        )

        chart_data = {"datasource_id": 0, "datasource_type": "table"}
        rv = self.put_assert_metric(f"/api/v1/chart/{chart.id}", chart_data, "put")
        self.assertEqual(rv.status_code, 422)
        response = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            response, {"message": {"datasource_id": ["Datasource does not exist"]}}
        )

        db.session.delete(chart)
        db.session.commit()

    def test_update_chart_validate_owners(self):
        """
        Chart API: Test update validate owners
        """
        chart_data = {
            "slice_name": "title1",
            "datasource_id": 1,
            "datasource_type": "table",
            "owners": [1000],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/"
        rv = self.client.post(uri, json=chart_data)
        self.assertEqual(rv.status_code, 422)
        response = json.loads(rv.data.decode("utf-8"))
        expected_response = {"message": {"owners": ["Owners are invalid"]}}
        self.assertEqual(response, expected_response)

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_get_chart(self):
        """
        Chart API: Test get chart
        """
        admin = self.get_user("admin")
        chart = self.insert_chart("title", [admin.id], 1)
        self.login(username="admin")
        uri = f"api/v1/chart/{chart.id}"
        rv = self.get_assert_metric(uri, "get")
        self.assertEqual(rv.status_code, 200)
        expected_result = {
            "cache_timeout": None,
            "certified_by": None,
            "certification_details": None,
            "dashboards": [],
            "description": None,
            "owners": [
                {
                    "id": 1,
                    "first_name": "admin",
                    "last_name": "user",
                }
            ],
            "params": None,
            "slice_name": "title",
            "tags": [],
            "viz_type": None,
            "query_context": None,
            "is_managed_externally": False,
        }
        data = json.loads(rv.data.decode("utf-8"))
        self.assertIn("changed_on_delta_humanized", data["result"])
        self.assertIn("id", data["result"])
        self.assertIn("thumbnail_url", data["result"])
        self.assertIn("url", data["result"])
        for key, value in data["result"].items():
            # We can't assert timestamp values or id/urls
            if key not in (
                "changed_on_delta_humanized",
                "id",
                "thumbnail_url",
                "url",
            ):
                self.assertEqual(value, expected_result[key])
        db.session.delete(chart)
        db.session.commit()

    def test_get_chart_not_found(self):
        """
        Chart API: Test get chart not found
        """
        chart_id = 1000
        self.login(username="admin")
        uri = f"api/v1/chart/{chart_id}"
        rv = self.get_assert_metric(uri, "get")
        self.assertEqual(rv.status_code, 404)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_get_chart_no_data_access(self):
        """
        Chart API: Test get chart without data access
        """
        self.login(username="gamma")
        chart_no_access = (
            db.session.query(Slice)
            .filter_by(slice_name="Girl Name Cloud")
            .one_or_none()
        )
        uri = f"api/v1/chart/{chart_no_access.id}"
        rv = self.client.get(uri)
        self.assertEqual(rv.status_code, 404)

    @pytest.mark.usefixtures(
        "load_energy_table_with_slice",
        "load_birth_names_dashboard_with_slices",
        "load_unicode_dashboard_with_slice",
        "load_world_bank_dashboard_with_slices",
    )
    def test_get_charts(self):
        """
        Chart API: Test get charts
        """
        self.login(username="admin")
        uri = f"api/v1/chart/"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 33)

    @pytest.mark.usefixtures("load_energy_table_with_slice", "add_dashboard_to_chart")
    def test_get_charts_dashboards(self):
        """
        Chart API: Test get charts with related dashboards
        """
        self.login(username="admin")
        arguments = {
            "filters": [
                {"col": "slice_name", "opr": "eq", "value": self.chart.slice_name}
            ]
        }
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        assert data["result"][0]["dashboards"] == [
            {
                "id": self.original_dashboard.id,
                "dashboard_title": self.original_dashboard.dashboard_title,
            }
        ]

    @pytest.mark.usefixtures("load_energy_table_with_slice", "add_dashboard_to_chart")
    def test_get_charts_dashboard_filter(self):
        """
        Chart API: Test get charts with dashboard filter
        """
        self.login(username="admin")
        arguments = {
            "filters": [
                {
                    "col": "dashboards",
                    "opr": "rel_m_m",
                    "value": self.original_dashboard.id,
                }
            ]
        }
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        result = data["result"]
        assert len(result) == 1
        assert result[0]["slice_name"] == self.chart.slice_name

    def test_get_charts_changed_on(self):
        """
        Dashboard API: Test get charts changed on
        """
        admin = self.get_user("admin")
        chart = self.insert_chart("foo_a", [admin.id], 1, description="ZY_bar")

        self.login(username="admin")

        arguments = {
            "order_column": "changed_on_delta_humanized",
            "order_direction": "desc",
        }
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"

        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        assert data["result"][0]["changed_on_delta_humanized"] in (
            "now",
            "a second ago",
        )

        # rollback changes
        db.session.delete(chart)
        db.session.commit()

    @pytest.mark.usefixtures(
        "load_world_bank_dashboard_with_slices",
        "load_birth_names_dashboard_with_slices",
    )
    def test_get_charts_filter(self):
        """
        Chart API: Test get charts filter
        """
        self.login(username="admin")
        arguments = {"filters": [{"col": "slice_name", "opr": "sw", "value": "G"}]}
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 5)

    @pytest.fixture()
    def load_energy_charts(self):
        with app.app_context():
            admin = self.get_user("admin")
            energy_table = (
                db.session.query(SqlaTable)
                .filter_by(table_name="energy_usage")
                .one_or_none()
            )
            energy_table_id = 1
            if energy_table:
                energy_table_id = energy_table.id
            chart1 = self.insert_chart(
                "foo_a", [admin.id], energy_table_id, description="ZY_bar"
            )
            chart2 = self.insert_chart(
                "zy_foo", [admin.id], energy_table_id, description="desc1"
            )
            chart3 = self.insert_chart(
                "foo_b", [admin.id], energy_table_id, description="desc1zy_"
            )
            chart4 = self.insert_chart(
                "foo_c", [admin.id], energy_table_id, viz_type="viz_zy_"
            )
            chart5 = self.insert_chart(
                "bar", [admin.id], energy_table_id, description="foo"
            )

            yield
            # rollback changes
            db.session.delete(chart1)
            db.session.delete(chart2)
            db.session.delete(chart3)
            db.session.delete(chart4)
            db.session.delete(chart5)
            db.session.commit()

    @pytest.mark.usefixtures("load_energy_charts")
    def test_get_charts_custom_filter(self):
        """
        Chart API: Test get charts custom filter
        """

        arguments = {
            "filters": [{"col": "slice_name", "opr": "chart_all_text", "value": "zy_"}],
            "order_column": "slice_name",
            "order_direction": "asc",
            "keys": ["none"],
            "columns": ["slice_name", "description", "viz_type"],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 4)

        expected_response = [
            {"description": "ZY_bar", "slice_name": "foo_a", "viz_type": None},
            {"description": "desc1zy_", "slice_name": "foo_b", "viz_type": None},
            {"description": None, "slice_name": "foo_c", "viz_type": "viz_zy_"},
            {"description": "desc1", "slice_name": "zy_foo", "viz_type": None},
        ]
        for index, item in enumerate(data["result"]):
            self.assertEqual(
                item["description"], expected_response[index]["description"]
            )
            self.assertEqual(item["slice_name"], expected_response[index]["slice_name"])
            self.assertEqual(item["viz_type"], expected_response[index]["viz_type"])

    @pytest.mark.usefixtures("load_energy_table_with_slice", "load_energy_charts")
    def test_admin_gets_filtered_energy_slices(self):
        # test filtering on datasource_name
        arguments = {
            "filters": [
                {
                    "col": "slice_name",
                    "opr": "chart_all_text",
                    "value": "energy",
                }
            ],
            "keys": ["none"],
            "columns": ["slice_name", "description", "table.table_name"],
        }
        self.login(username="admin")

        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        data = rv.json
        assert rv.status_code == 200
        assert data["count"] > 0
        for chart in data["result"]:
            print(chart)
            assert (
                "energy"
                in " ".join(
                    [
                        chart["slice_name"] or "",
                        chart["description"] or "",
                        chart["table"]["table_name"] or "",
                    ]
                ).lower()
            )

    @pytest.mark.usefixtures("create_certified_charts")
    def test_gets_certified_charts_filter(self):
        arguments = {
            "filters": [
                {
                    "col": "id",
                    "opr": "chart_is_certified",
                    "value": True,
                }
            ],
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="admin")

        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], CHARTS_FIXTURE_COUNT)

    @pytest.mark.usefixtures("create_charts")
    def test_gets_not_certified_charts_filter(self):
        arguments = {
            "filters": [
                {
                    "col": "id",
                    "opr": "chart_is_certified",
                    "value": False,
                }
            ],
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="admin")

        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 17)

    @pytest.mark.usefixtures("load_energy_charts")
    def test_user_gets_none_filtered_energy_slices(self):
        # test filtering on datasource_name
        arguments = {
            "filters": [
                {
                    "col": "slice_name",
                    "opr": "chart_all_text",
                    "value": "energy",
                }
            ],
            "keys": ["none"],
            "columns": ["slice_name"],
        }

        self.login(username="gamma")
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 0)

    @pytest.mark.usefixtures("create_charts")
    def test_get_charts_favorite_filter(self):
        """
        Chart API: Test get charts favorite filter
        """
        admin = self.get_user("admin")
        users_favorite_query = db.session.query(FavStar.obj_id).filter(
            and_(FavStar.user_id == admin.id, FavStar.class_name == "slice")
        )
        expected_models = (
            db.session.query(Slice)
            .filter(and_(Slice.id.in_(users_favorite_query)))
            .order_by(Slice.slice_name.asc())
            .all()
        )

        arguments = {
            "filters": [{"col": "id", "opr": "chart_is_favorite", "value": True}],
            "order_column": "slice_name",
            "order_direction": "asc",
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="admin")
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert len(expected_models) == data["count"]

        for i, expected_model in enumerate(expected_models):
            assert expected_model.slice_name == data["result"][i]["slice_name"]

        # Test not favorite charts
        expected_models = (
            db.session.query(Slice)
            .filter(and_(~Slice.id.in_(users_favorite_query)))
            .order_by(Slice.slice_name.asc())
            .all()
        )
        arguments["filters"][0]["value"] = False
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert len(expected_models) == data["count"]

    @pytest.mark.usefixtures("create_charts_created_by_gamma")
    def test_get_charts_created_by_me_filter(self):
        """
        Chart API: Test get charts with created by me special filter
        """
        gamma_user = self.get_user("gamma")
        expected_models = (
            db.session.query(Slice).filter(Slice.created_by_fk == gamma_user.id).all()
        )
        arguments = {
            "filters": [
                {"col": "created_by", "opr": "chart_created_by_me", "value": "me"}
            ],
            "order_column": "slice_name",
            "order_direction": "asc",
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="gamma")
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert len(expected_models) == data["count"]
        for i, expected_model in enumerate(expected_models):
            assert expected_model.slice_name == data["result"][i]["slice_name"]

    @pytest.mark.usefixtures("create_charts")
    def test_get_current_user_favorite_status(self):
        """
        Dataset API: Test get current user favorite stars
        """
        admin = self.get_user("admin")
        users_favorite_ids = [
            star.obj_id
            for star in db.session.query(FavStar.obj_id)
            .filter(
                and_(
                    FavStar.user_id == admin.id,
                    FavStar.class_name == FavStarClassName.CHART,
                )
            )
            .all()
        ]

        assert users_favorite_ids
        arguments = [s.id for s in db.session.query(Slice.id).all()]
        self.login(username="admin")
        uri = f"api/v1/chart/favorite_status/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        for res in data["result"]:
            if res["id"] in users_favorite_ids:
                assert res["value"]

    def test_add_favorite(self):
        """
        Dataset API: Test add chart to favorites
        """
        chart = Slice(
            id=100,
            datasource_id=1,
            datasource_type="table",
            datasource_name="tmp_perm_table",
            slice_name="slice_name",
        )
        db.session.add(chart)
        db.session.commit()

        self.login(username="admin")
        uri = f"api/v1/chart/favorite_status/?q={prison.dumps([chart.id])}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        for res in data["result"]:
            assert res["value"] is False

        uri = f"api/v1/chart/{chart.id}/favorites/"
        self.client.post(uri)

        uri = f"api/v1/chart/favorite_status/?q={prison.dumps([chart.id])}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        for res in data["result"]:
            assert res["value"] is True

        db.session.delete(chart)
        db.session.commit()

    def test_remove_favorite(self):
        """
        Dataset API: Test remove chart from favorites
        """
        chart = Slice(
            id=100,
            datasource_id=1,
            datasource_type="table",
            datasource_name="tmp_perm_table",
            slice_name="slice_name",
        )
        db.session.add(chart)
        db.session.commit()

        self.login(username="admin")
        uri = f"api/v1/chart/{chart.id}/favorites/"
        self.client.post(uri)

        uri = f"api/v1/chart/favorite_status/?q={prison.dumps([chart.id])}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        for res in data["result"]:
            assert res["value"] is True

        uri = f"api/v1/chart/{chart.id}/favorites/"
        self.client.delete(uri)

        uri = f"api/v1/chart/favorite_status/?q={prison.dumps([chart.id])}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        for res in data["result"]:
            assert res["value"] is False

        db.session.delete(chart)
        db.session.commit()

    def test_get_time_range(self):
        """
        Chart API: Test get actually time range from human readable string
        """
        self.login(username="admin")
        humanize_time_range = "100 years ago : now"
        uri = f"api/v1/time_range/?q={prison.dumps(humanize_time_range)}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(data["result"]), 3)

    def test_query_form_data(self):
        """
        Chart API: Test query form data
        """
        self.login(username="admin")
        slice = db.session.query(Slice).first()
        uri = f"api/v1/form_data/?slice_id={slice.id if slice else None}"
        rv = self.client.get(uri)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.content_type, "application/json")
        if slice:
            self.assertEqual(data["slice_id"], slice.id)

    @pytest.mark.usefixtures(
        "load_unicode_dashboard_with_slice",
        "load_energy_table_with_slice",
        "load_world_bank_dashboard_with_slices",
        "load_birth_names_dashboard_with_slices",
    )
    def test_get_charts_page(self):
        """
        Chart API: Test get charts filter
        """
        # Assuming we have 33 sample charts
        self.login(username="admin")
        arguments = {"page_size": 10, "page": 0}
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(len(data["result"]), 10)

        arguments = {"page_size": 10, "page": 3}
        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(len(data["result"]), 3)

    def test_get_charts_no_data_access(self):
        """
        Chart API: Test get charts no data access
        """
        self.login(username="gamma")
        uri = "api/v1/chart/"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 0)

    def test_export_chart(self):
        """
        Chart API: Test export chart
        """
        example_chart = db.session.query(Slice).all()[0]
        argument = [example_chart.id]
        uri = f"api/v1/chart/export/?q={prison.dumps(argument)}"

        self.login(username="admin")
        rv = self.get_assert_metric(uri, "export")

        assert rv.status_code == 200

        buf = BytesIO(rv.data)
        assert is_zipfile(buf)

    def test_export_chart_not_found(self):
        """
        Chart API: Test export chart not found
        """
        # Just one does not exist and we get 404
        argument = [-1, 1]
        uri = f"api/v1/chart/export/?q={prison.dumps(argument)}"
        self.login(username="admin")
        rv = self.get_assert_metric(uri, "export")

        assert rv.status_code == 404

    def test_export_chart_gamma(self):
        """
        Chart API: Test export chart has gamma
        """
        example_chart = db.session.query(Slice).all()[0]
        argument = [example_chart.id]
        uri = f"api/v1/chart/export/?q={prison.dumps(argument)}"

        self.login(username="gamma")
        rv = self.client.get(uri)

        assert rv.status_code == 404

    def test_import_chart(self):
        """
        Chart API: Test import chart
        """
        self.login(username="admin")
        uri = "api/v1/chart/import/"

        buf = self.create_chart_import()
        form_data = {
            "formData": (buf, "chart_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        database = (
            db.session.query(Database).filter_by(uuid=database_config["uuid"]).one()
        )
        assert database.database_name == "imported_database"

        assert len(database.tables) == 1
        dataset = database.tables[0]
        assert dataset.table_name == "imported_dataset"
        assert str(dataset.uuid) == dataset_config["uuid"]

        chart = db.session.query(Slice).filter_by(uuid=chart_config["uuid"]).one()
        assert chart.table == dataset

        db.session.delete(chart)
        db.session.commit()
        db.session.delete(dataset)
        db.session.commit()
        db.session.delete(database)
        db.session.commit()

    def test_import_chart_overwrite(self):
        """
        Chart API: Test import existing chart
        """
        self.login(username="admin")
        uri = "api/v1/chart/import/"

        buf = self.create_chart_import()
        form_data = {
            "formData": (buf, "chart_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        # import again without overwrite flag
        buf = self.create_chart_import()
        form_data = {
            "formData": (buf, "chart_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 422
        assert response == {
            "errors": [
                {
                    "message": "Error importing chart",
                    "error_type": "GENERIC_COMMAND_ERROR",
                    "level": "warning",
                    "extra": {
                        "charts/imported_chart.yaml": "Chart already exists and `overwrite=true` was not passed",
                        "issue_codes": [
                            {
                                "code": 1010,
                                "message": "Issue 1010 - Superset encountered an error while running a command.",
                            }
                        ],
                    },
                }
            ]
        }

        # import with overwrite flag
        buf = self.create_chart_import()
        form_data = {
            "formData": (buf, "chart_export.zip"),
            "overwrite": "true",
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        # clean up
        database = (
            db.session.query(Database).filter_by(uuid=database_config["uuid"]).one()
        )
        dataset = database.tables[0]
        chart = db.session.query(Slice).filter_by(uuid=chart_config["uuid"]).one()

        db.session.delete(chart)
        db.session.commit()
        db.session.delete(dataset)
        db.session.commit()
        db.session.delete(database)
        db.session.commit()

    def test_import_chart_invalid(self):
        """
        Chart API: Test import invalid chart
        """
        self.login(username="admin")
        uri = "api/v1/chart/import/"

        buf = BytesIO()
        with ZipFile(buf, "w") as bundle:
            with bundle.open("chart_export/metadata.yaml", "w") as fp:
                fp.write(yaml.safe_dump(dataset_metadata_config).encode())
            with bundle.open(
                "chart_export/databases/imported_database.yaml", "w"
            ) as fp:
                fp.write(yaml.safe_dump(database_config).encode())
            with bundle.open("chart_export/datasets/imported_dataset.yaml", "w") as fp:
                fp.write(yaml.safe_dump(dataset_config).encode())
            with bundle.open("chart_export/charts/imported_chart.yaml", "w") as fp:
                fp.write(yaml.safe_dump(chart_config).encode())
        buf.seek(0)

        form_data = {
            "formData": (buf, "chart_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 422
        assert response == {
            "errors": [
                {
                    "message": "Error importing chart",
                    "error_type": "GENERIC_COMMAND_ERROR",
                    "level": "warning",
                    "extra": {
                        "metadata.yaml": {"type": ["Must be equal to Slice."]},
                        "issue_codes": [
                            {
                                "code": 1010,
                                "message": (
                                    "Issue 1010 - Superset encountered an "
                                    "error while running a command."
                                ),
                            }
                        ],
                    },
                }
            ]
        }

    def test_gets_created_by_user_charts_filter(self):
        arguments = {
            "filters": [{"col": "id", "opr": "chart_has_created_by", "value": True}],
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="admin")

        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 8)

    def test_gets_not_created_by_user_charts_filter(self):
        arguments = {
            "filters": [{"col": "id", "opr": "chart_has_created_by", "value": False}],
            "keys": ["none"],
            "columns": ["slice_name"],
        }
        self.login(username="admin")

        uri = f"api/v1/chart/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data["count"], 8)

    @pytest.mark.usefixtures("create_charts")
    def test_gets_owned_created_favorited_by_me_filter(self):
        """
        Chart API: Test ChartOwnedCreatedFavoredByMeFilter
        """
        self.login(username="admin")
        arguments = {
            "filters": [
                {
                    "col": "id",
                    "opr": "chart_owned_created_favored_by_me",
                    "value": True,
                }
            ],
            "order_column": "slice_name",
            "order_direction": "asc",
            "page": 0,
            "page_size": 25,
        }
        rv = self.client.get(f"api/v1/chart/?q={prison.dumps(arguments)}")
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))

        assert data["result"][0]["slice_name"] == "name0"
        assert data["result"][0]["datasource_id"] == 1

    @parameterized.expand(
        [
            "Top 10 Girl Name Share",  # Legacy chart
            "Pivot Table v2",  # Non-legacy chart
        ],
    )
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_warm_up_cache(self, slice_name):
        self.login()
        slc = self.get_slice(slice_name, db.session)
        rv = self.client.put("/api/v1/chart/warm_up_cache", json={"chart_id": slc.id})
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))

        self.assertEqual(
            data["result"],
            [{"chart_id": slc.id, "viz_error": None, "viz_status": "success"}],
        )

        dashboard = self.get_dash_by_slug("births")

        rv = self.client.put(
            "/api/v1/chart/warm_up_cache",
            json={"chart_id": slc.id, "dashboard_id": dashboard.id},
        )
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            data["result"],
            [{"chart_id": slc.id, "viz_error": None, "viz_status": "success"}],
        )

        rv = self.client.put(
            "/api/v1/chart/warm_up_cache",
            json={
                "chart_id": slc.id,
                "dashboard_id": dashboard.id,
                "extra_filters": json.dumps(
                    [{"col": "name", "op": "in", "val": ["Jennifer"]}]
                ),
            },
        )
        self.assertEqual(rv.status_code, 200)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            data["result"],
            [{"chart_id": slc.id, "viz_error": None, "viz_status": "success"}],
        )

    def test_warm_up_cache_chart_id_required(self):
        self.login()
        rv = self.client.put("/api/v1/chart/warm_up_cache", json={"dashboard_id": 1})
        self.assertEqual(rv.status_code, 400)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            data,
            {"message": {"chart_id": ["Missing data for required field."]}},
        )

    def test_warm_up_cache_chart_not_found(self):
        self.login()
        rv = self.client.put("/api/v1/chart/warm_up_cache", json={"chart_id": 99999})
        self.assertEqual(rv.status_code, 404)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(data, {"message": "Chart not found"})

    def test_warm_up_cache_payload_validation(self):
        self.login()
        rv = self.client.put(
            "/api/v1/chart/warm_up_cache",
            json={"chart_id": "id", "dashboard_id": "id", "extra_filters": 4},
        )
        self.assertEqual(rv.status_code, 400)
        data = json.loads(rv.data.decode("utf-8"))
        self.assertEqual(
            data,
            {
                "message": {
                    "chart_id": ["Not a valid integer."],
                    "dashboard_id": ["Not a valid integer."],
                    "extra_filters": ["Not a valid string."],
                }
            },
        )

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_warm_up_cache_error(self) -> None:
        self.login()
        slc = self.get_slice("Pivot Table v2", db.session)

        with mock.patch.object(ChartDataCommand, "run") as mock_run:
            mock_run.side_effect = ChartDataQueryFailedError(
                _(
                    "Error: %(error)s",
                    error=_("Empty query?"),
                )
            )

            assert json.loads(
                self.client.put(
                    "/api/v1/chart/warm_up_cache",
                    json={"chart_id": slc.id},
                ).data
            ) == {
                "result": [
                    {
                        "chart_id": slc.id,
                        "viz_error": "Error: Empty query?",
                        "viz_status": None,
                    },
                ],
            }

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_warm_up_cache_no_query_context(self) -> None:
        self.login()
        slc = self.get_slice("Pivot Table v2", db.session)

        with mock.patch.object(Slice, "get_query_context") as mock_get_query_context:
            mock_get_query_context.return_value = None

            assert json.loads(
                self.client.put(
                    f"/api/v1/chart/warm_up_cache",
                    json={"chart_id": slc.id},
                ).data
            ) == {
                "result": [
                    {
                        "chart_id": slc.id,
                        "viz_error": "Chart's query context does not exist",
                        "viz_status": None,
                    },
                ],
            }

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_warm_up_cache_no_datasource(self) -> None:
        self.login()
        slc = self.get_slice("Top 10 Girl Name Share", db.session)

        with mock.patch.object(
            Slice,
            "datasource",
            new_callable=mock.PropertyMock,
        ) as mock_datasource:
            mock_datasource.return_value = None

            assert json.loads(
                self.client.put(
                    f"/api/v1/chart/warm_up_cache",
                    json={"chart_id": slc.id},
                ).data
            ) == {
                "result": [
                    {
                        "chart_id": slc.id,
                        "viz_error": "Chart's datasource does not exist",
                        "viz_status": None,
                    },
                ],
            }
