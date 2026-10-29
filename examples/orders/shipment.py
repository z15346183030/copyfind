# Copyright 2026 Example Systems Ltd.
# Licensed under the MIT licence, see LICENSE.
# This file is part of the demo, not of copyfind itself.


def validate_line(item):
    if not isinstance(item, dict):
        raise TypeError("line must be a dict")
    quantity = item.get("quantity", 0)
    price = item.get("price", 0)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if price < 0:
        raise ValueError("price must not be negative")
    return quantity * price


def shipment_weight(lines):
    grams = 0
    for line in lines:
        grams += validate_line(line) * 100
    return grams
