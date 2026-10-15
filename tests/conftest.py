"""Shared fixtures for E.ON Next Prepay tests."""

from __future__ import annotations

import base64
import json
import time

import pytest

ACCOUNT = "A-TEST1234"
GAS_DEVICE = "00-11-22-33-44-55-66-77"
ELEC_DEVICE = "AA-BB-CC-DD-EE-FF-00-11"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield


def make_jwt(exp_in: float = 3600) -> str:
    """An unsigned JWT with an exp claim (the client never verifies signatures)."""

    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': time.time() + exp_in})}.sig"


def accounts_payload() -> list[dict]:
    """Account tree: prepay gas meter, credit-mode electricity meter, plus an IHD."""
    return [
        {
            "number": ACCOUNT,
            "properties": [
                {
                    "electricityMeterPoints": [
                        {
                            "mpan": "1900000000001",
                            "meters": [
                                {
                                    "serialNumber": "E1",
                                    "consumptionUnits": "kWh",
                                    "smartDevices": [
                                        {
                                            "deviceId": ELEC_DEVICE,
                                            "type": "ESME",
                                            "paymentMode": "CREDIT",
                                            "weeklyDebtRecoveryRateInPence": None,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "gasMeterPoints": [
                        {
                            "mprn": "1234567890",
                            "meters": [
                                {
                                    "serialNumber": "G1",
                                    "consumptionUnits": "m³",
                                    "smartDevices": [
                                        {
                                            "deviceId": GAS_DEVICE,
                                            "type": "GSME",
                                            "paymentMode": "PREPAY",
                                            "weeklyDebtRecoveryRateInPence": 350,
                                        },
                                        {
                                            "deviceId": "IHD-1",
                                            "type": "IHD",
                                            "paymentMode": "PREPAY",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]
