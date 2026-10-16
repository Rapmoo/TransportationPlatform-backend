import random
import uuid

from locust import HttpUser, between, task


LOCATION_TYPES = [
    "HOME",
    "STARTING_POINT",
    "BOARDING",
    "RETURN_STARTING_POINT",
]


class WorkerSubmissionUser(HttpUser):
    # Pause between completed form flows, not between individual requests.
    wait_time = between(1, 3)

    @task
    def submit_worker_form(self):
        employee_id = f"LOAD-{uuid.uuid4().hex[:12].upper()}"
        public_bus_id = "".join(
            random.choices("0123456789", k=16)
        )

        # Request 1: Create a draft and obtain the worker token.
        with self.client.post(
            "/api/submissions/",
            json={
                "employee_id": employee_id,
                "employee_name": "Load Test Worker",
                "organization_name": "LoadTestOrg",
                "public_bus_id": public_bus_id,
                "bus_number_going": "A1",
                "bus_number_return": "A2",
            },
            name="1. Create draft",
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 201):
                response.failure(
                    f"Draft creation failed: HTTP {response.status_code}"
                )
                return

            try:
                data = response.json()
                public_id = data["public_id"]
                worker_token = data["worker_token"]
            except (ValueError, KeyError, TypeError) as exc:
                response.failure(f"Invalid draft response: {exc}")
                return

            response.success()

        headers = {
            "Authorization": f"Bearer {worker_token}"
        }

        # Requests 2–5: Submit the four required locations.
        for index, location_type in enumerate(LOCATION_TYPES):
            with self.client.post(
                f"/api/submissions/{public_id}/locations",
                headers=headers,
                json={
                    "location_type": location_type,
                    "address_text": f"Load test address {location_type}",
                    "latitude": 9.0 + index * 0.01,
                    "longitude": 38.7 + index * 0.01,
                },
                name="2-5. Save location",
                catch_response=True,
            ) as response:
                if response.status_code not in (200, 201):
                    response.failure(
                        f"{location_type} failed: HTTP {response.status_code}"
                    )
                    return

                response.success()

        # Request 6: Finalize the submission.
        with self.client.post(
            f"/api/submissions/{public_id}/submit",
            headers=headers,
            name="6. Submit form",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(
                    f"Final submission failed: HTTP {response.status_code}"
                )
                return

            response.success()
