
"""Read-only test of Kalshi's CF Benchmarks price feed."""

import os
import time
import base64
import requests

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes


key_id = os.environ["KALSHI_API_KEY_ID"]
private_key_text = os.environ["KALSHI_PRIVATE_KEY"]

private_key = serialization.load_pem_private_key(
    private_key_text.encode("utf-8"),
    password=None,
)

timestamp = str(int(time.time() * 1000))
path = "/trade-api/v2/cfbenchmarks/values"
message = (timestamp + "GET" + path).encode("utf-8")

if isinstance(private_key, Ed25519PrivateKey):
    signature = private_key.sign(message)
else:
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )

headers = {
    "KALSHI-ACCESS-KEY": key_id,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
}

url = "https://external-api.kalshi.com" + path

print("Testing Kalshi's Bitcoin price feed...")

response = requests.get(
    url,
    params={"id": "BRTI"},
    headers=headers,
    timeout=15,
)

print("HTTP status:", response.status_code)

if response.ok:
    print("SUCCESS: Kalshi accepted the price request.")
    print("Response:", response.text[:1500])
else:
    print("PRICE FEED UNAVAILABLE")
    print("Response:", response.text[:500])
