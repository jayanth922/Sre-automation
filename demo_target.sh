#!/bin/bash
# =============================================================================
# Demo target — a self-contained sample workload (microservices + Prometheus +
# Loki) that stands in for a customer's cluster during local testing.
#
# This is NOT part of the platform. It runs independently so you can point the
# platform at it exactly as you would point it at any real customer cluster.
#
#   ./demo_target.sh          bring the sample workload up
#   ./demo_target.sh --down   tear it down
# =============================================================================
set -e
bash Target_Client/start.sh "$@"
