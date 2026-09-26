"""Parcel fare calculation (synthetic fixture)."""
from brambleway.zones import zone_for

BASE = {"A": 4.50, "B": 6.25, "C": 9.80}


def fare(weight_kg, postcode, express=False):
    zone = zone_for(postcode)
    price = BASE[zone] + 1.10 * weight_kg
    if express:
        price *= 1.5
    if weight_kg > 30:
        price += 12.0
    return round(price, 2)


def bulk_discount(total, parcels):
    if parcels >= 50:
        return round(total * 0.85, 2)
    if parcels >= 10:
        return round(total * 0.93, 2)
    return total
