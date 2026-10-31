"""Offline boundaries of the read-only authorization review helper."""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import review_read as read


class ReviewReadTests(unittest.TestCase):
    def test_only_get_and_test_permissions_are_allowed(self):
        target = "https://iam.googleapis.com/v1/projects/p/serviceAccounts/a:testIamPermissions"
        read.validate_request("POST", target, ["iam.serviceAccounts.actAs"])
        for method, url, permissions in (
            ("DELETE", target, None),
            (
                "POST",
                "https://run.googleapis.com/v2/projects/p/locations/r/jobs",
                ["run.jobs.create"],
            ),
            ("GET", "https://run.googleapis.com/v2/projects/p/locations/r/jobs/j:run", None),
            ("GET", "https://iam.googleapis.com@elsewhere.invalid/v1/projects/p", None),
            ("GET", "http://iam.googleapis.com/v1/projects/p", None),
            ("POST", target, []),
        ):
            with self.subTest(method=method, url=url), self.assertRaises(RuntimeError):
                read.validate_request(method, url, permissions)

    def test_invalid_request_fails_before_credentials(self):
        with patch.object(read, "gc") as credentials:
            with self.assertRaises(RuntimeError):
                read.request("POST", "https://run.googleapis.com/v2/jobs/j:run", [])
        credentials.assert_not_called()

    def test_allowed_post_has_only_permission_query_not_mutation_payload(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"permissions": ["iam.serviceAccounts.actAs"]}).encode()

        with patch.object(read, "gc", return_value="fake-token"):
            with patch("review_read.urllib.request.build_opener") as factory:
                factory.return_value.open.return_value = Response()
                read.request(
                    "POST",
                    "https://iam.googleapis.com/v1/projects/p/serviceAccounts/a:testIamPermissions",
                    ["iam.serviceAccounts.actAs"],
                )
        req = factory.return_value.open.call_args.args[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.data), {"permissions": ["iam.serviceAccounts.actAs"]})

    def test_denied_disabled_and_missing_are_not_silent_absence(self):
        for code, status, reason in (
            (403, "PERMISSION_DENIED", "IAM_PERMISSION_DENIED"),
            (403, "PERMISSION_DENIED", "SERVICE_DISABLED"),
            (404, "NOT_FOUND", "RESOURCE_NOT_FOUND"),
        ):
            body = {
                "error": {
                    "status": status,
                    "message": "private-fixture",
                    "details": [{"reason": reason}],
                }
            }
            error = urllib.error.HTTPError(
                "https://iam.googleapis.com",
                code,
                "error",
                {},
                io.BytesIO(json.dumps(body).encode()),
            )
            with patch.object(read, "gc", return_value="fake-token"):
                with patch("review_read.urllib.request.build_opener") as factory:
                    factory.return_value.open.side_effect = error
                    with self.assertRaisesRegex(RuntimeError, reason) as captured:
                        read.request(
                            "GET", "https://iam.googleapis.com/v1/projects/p/serviceAccounts/a"
                        )
            self.assertNotIn("private-fixture", str(captured.exception))
            self.assertNotIn("fake-token", str(captured.exception))

    def test_pagination_is_complete_and_repeated_tokens_stop(self):
        with patch.object(
            read,
            "request",
            side_effect=[
                {"constraints": [1], "nextPageToken": "next"},
                {"constraints": [2]},
            ],
        ):
            self.assertEqual(
                read.read_pages(
                    "https://orgpolicy.googleapis.com/v2/projects/p/constraints", "constraints"
                ),
                {"constraints": [1, 2], "inventory_complete": True},
            )
        with patch.object(read, "request", return_value={"nextPageToken": "same"}):
            with self.assertRaisesRegex(RuntimeError, "Repeated"):
                read.read_pages(
                    "https://orgpolicy.googleapis.com/v2/projects/p/constraints", "constraints"
                )


if __name__ == "__main__":
    unittest.main()
