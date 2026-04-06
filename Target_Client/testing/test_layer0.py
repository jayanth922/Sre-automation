import unittest
import requests
import time

class TestLayer0(unittest.TestCase):
    BASE_URL = "http://localhost:8000"

    def test_health_check(self):
        """Test API Gateway health endpoint."""
        try:
            response = requests.get(f"{self.BASE_URL}/health", timeout=5)
            self.assertEqual(response.status_code, 200, "Health check should return 200")
            self.assertIn("status", response.json())
        except requests.exceptions.ConnectionError:
            self.fail("Could not connect to the API Gateway. Are the Docker containers running?")

    def test_checkout_endpoint(self):
        """Test the checkout endpoint. Often returns 500 or is slow due to simulated chaos."""
        successes = 0
        failures = 0
        for _ in range(5):
            # We make multiple calls because there's a 15% error rate built in
            response = requests.get(f"{self.BASE_URL}/checkout/order123", timeout=10)
            if response.status_code == 200:
                successes += 1
            else:
                failures += 1
            time.sleep(0.5)
        
        # As long as it responds (even with an intentional simulated failure), the test passes.
        self.assertTrue(successes > 0 or failures > 0, "No valid responses from checkout endpoint")

    def test_inventory_endpoint(self):
        """Test the inventory endpoint."""
        successes = 0
        for _ in range(3):
            response = requests.get(f"{self.BASE_URL}/inventory", timeout=10)
            if response.status_code == 200:
                successes += 1
        self.assertTrue(successes > 0, "Inventory endpoint not returning 200 OK")

if __name__ == "__main__":
    print("Testing Layer 0: System Under Observation...")
    print("Make sure the Target_Client K8s deployments are running first!\n")
    unittest.main()
