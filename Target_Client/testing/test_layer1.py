import unittest
import requests
import time

class TestLayer1(unittest.TestCase):
    PROMETHEUS_URL = "http://localhost:9090"
    ALERTMANAGER_URL = "http://localhost:9093"
    LOKI_URL = "http://localhost:3100"

    def test_prometheus_health(self):
        """Test if Prometheus is running."""
        try:
            response = requests.get(f"{self.PROMETHEUS_URL}/-/healthy", timeout=5)
            self.assertEqual(response.status_code, 200, "Prometheus health check failed")
        except requests.exceptions.ConnectionError:
            self.fail("Could not connect to Prometheus. Is the container running?")

    def test_prometheus_scrape_targets(self):
        """Verify Prometheus has successfully found and scraped our 3 microservices."""
        # Wait a few seconds to let Prometheus discover targets if just booted
        response = requests.get(f"{self.PROMETHEUS_URL}/api/v1/targets", timeout=5)
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        active_targets = data.get("data", {}).get("activeTargets", [])
        
        # We expect to find 'api-gateway', 'checkout-service', 'inventory-service', plus prometheus itself
        found_jobs = [target.get("discoveredLabels", {}).get("job") for target in active_targets]
        
        self.assertIn("api-gateway", found_jobs, "Prometheus is not scraping api-gateway")
        self.assertIn("checkout-service", found_jobs, "Prometheus is not scraping checkout-service")
        self.assertIn("inventory-service", found_jobs, "Prometheus is not scraping inventory-service")

        # Verify their health
        for target in active_targets:
            if target.get("discoveredLabels", {}).get("job") in ["api-gateway", "checkout-service", "inventory-service"]:
                self.assertEqual(target.get("health"), "up", f"Target {target['discoveredLabels']['job']} is down!")

    def test_alertmanager_health(self):
        """Test if Alertmanager is running."""
        try:
            response = requests.get(f"{self.ALERTMANAGER_URL}/-/healthy", timeout=5)
            self.assertEqual(response.status_code, 200, "Alertmanager health check failed")
        except requests.exceptions.ConnectionError:
            self.fail("Could not connect to Alertmanager. Is the container running?")
            
    def test_loki_health(self):
         """Test if Loki is running. Loki sometimes takes 15-30 seconds to be ready."""
         for _ in range(5):
             try:
                 response = requests.get(f"{self.LOKI_URL}/ready", timeout=5)
                 if response.status_code == 200:
                     return # Pass!
                 time.sleep(3)
             except requests.exceptions.ConnectionError:
                 time.sleep(3)
                 
         self.fail("Could not connect to Loki or it never became ready (returned 200).")


if __name__ == "__main__":
    print("Testing Layer 1: Observability and Telemetry...")
    print("Make sure you booted the observability tools via Docker Compose first!\n")
    unittest.main()
