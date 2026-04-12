"""Wrapper that applies rate limiting patch then starts Hermes gateway."""
import sys

# Apply monkey-patch before Hermes loads
sys.path.insert(0, "/app")
from rate_limit import apply_patch
apply_patch()

# Now run the gateway
from hermes_cli.gateway import run_gateway
run_gateway(verbose=0)
