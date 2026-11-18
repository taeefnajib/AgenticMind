"""Deterministic sample sales database (fictional) for templates, tests and demos."""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

COUNTRIES = ["Germany", "France", "Bangladesh", "United States", "Japan", "Brazil", "India", "Canada"]
SEGMENTS = ["Enterprise", "SMB", "Consumer"]
PRODUCTS = [
    ("Aqua 750 Bottle", "Drinkware", 29.0), ("Aqua 500 Bottle", "Drinkware", 24.0), ("Thermo Mug", "Drinkware", 19.5),
    ("Trail Backpack 30L", "Bags", 89.0), ("City Tote", "Bags", 45.0), ("Laptop Sleeve", "Bags", 35.0),
    ("Desk Lamp", "Office", 59.0), ("Ergo Chair", "Office", 329.0), ("Standing Mat", "Office", 49.0),
    ("Noise-cancelling Headphones", "Electronics", 199.0), ("USB-C Hub", "Electronics", 39.0),
    ("Wireless Mouse", "Electronics", 29.0), ("Notebook A5", "Stationery", 7.5), ("Fountain Pen", "Stationery", 49.0),
    ("Sticky Notes", "Stationery", 3.5),
]  # fmt: skip


def build_sample_database(path: Path | str, *, seed: int = 42) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL,
                                segment TEXT NOT NULL, signup_date TEXT NOT NULL);
        CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                               unit_price REAL NOT NULL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                             order_date TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE order_items (order_id INTEGER NOT NULL REFERENCES orders(id),
                                  product_id INTEGER NOT NULL REFERENCES products(id),
                                  quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
                                  PRIMARY KEY (order_id, product_id));
        """
    )
    start = date(2025, 1, 1)
    for i in range(1, 41):
        signup = start - timedelta(days=rng.randint(0, 700))
        conn.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?)",
            (i, f"Customer {i:02d}", rng.choice(COUNTRIES), rng.choice(SEGMENTS), signup.isoformat()),
        )
    for i, (name, category, price) in enumerate(PRODUCTS, start=1):
        conn.execute("INSERT INTO products VALUES (?, ?, ?, ?)", (i, name, category, price))
    for order_id in range(1, 601):
        day = start + timedelta(days=rng.randint(0, 546))  # 2025-01-01 .. 2026-06-30
        status = rng.choices(["shipped", "delivered", "cancelled", "pending"], [30, 55, 8, 7])[0]
        conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", (order_id, rng.randint(1, 40), day.isoformat(), status))
        for product_id in rng.sample(range(1, len(PRODUCTS) + 1), rng.randint(1, 4)):
            price = PRODUCTS[product_id - 1][2]
            discount = rng.choice([1.0, 1.0, 1.0, 0.9])
            conn.execute(
                "INSERT INTO order_items VALUES (?, ?, ?, ?)",
                (order_id, product_id, rng.randint(1, 5), round(price * discount, 2)),
            )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return path
