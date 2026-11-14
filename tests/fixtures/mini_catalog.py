import csv
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _prod(
    pid,
    fi,
    en,
    brand,
    vendor,
    category,
    sales_unit,
    base_unit,
    size_in_base,
    price,
    avail=5000.0,
    rel=0.98,
    co2=1.20,
    vol_l=None,
    wt_kg=None,
    wt_g=None,
    allergens=(),
    may_allergens=(),
    claims=(),
    labels=("FINLAND",),
    origin="FI",
    temp=4,
):
    classes = []
    al_vals = [{"id": a, "unit": "CONTAINS"} for a in allergens] + [
        {"id": a, "unit": "MAY_CONTAIN"} for a in may_allergens
    ]
    if al_vals:
        classes.append({"name": "allergen", "values": al_vals})
    if claims:
        classes.append({"name": "nutritionalClaim", "values": [{"id": c} for c in claims]})
    if labels:
        classes.append({"name": "packagingMarkedLabel", "values": [{"id": l} for l in labels]})
    conv = {"synkkaPackagingType": "BASE_UNIT_OR_EACH"}
    if vol_l is not None:
        conv["volume"] = {"value": float(vol_l), "unit": "ltr"}
    elif wt_kg is not None:
        conv["netWeight"] = {"value": float(wt_kg), "unit": "KGM"}
    elif wt_g is not None:
        conv["netWeight"] = {"value": float(wt_g), "unit": "GRM"}
    synkka = {
        "id": str(pid),
        "deleted": False,
        "salesUnit": sales_unit,
        "baseUnit": base_unit,
        "vendorName": vendor,
        "category": category,
        "countryOfOrigin": origin,
        "temperatureCondition": temp,
        "units": [{"unitId": sales_unit, "sizeInBaseUnits": float(size_in_base)}],
        "synkkaData": {
            "names": [
                {"language": "fi", "value": fi},
                {"language": "en", "value": en},
                {"language": "sv", "value": en},
            ],
            "brand": brand,
            "classifications": classes,
            "unitConversions": [conv],
        },
    }
    return {
        "synkka": synkka,
        "price": float(price),
        "avail": float(avail),
        "rel": float(rel),
        "co2": float(co2),
        "sales_unit": sales_unit,
    }


CATALOG_SPECS = [
    _prod(
        "464073",
        "Valio Eila 1 l laktoositon kevytmaito",
        "Valio Eila 1l lactose free semi-skimmed milk",
        "Valio",
        "Valio Oy",
        "10400",
        "ST",
        "ST",
        1.0,
        1.64,
        avail=5000.0,
        rel=0.99,
        co2=1.12,
        vol_l=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "464201",
        "Valio kevytmaito 1 l laktoositon UHT",
        "Valio semi-skimmed milk 1l lactose free UHT",
        "Valio",
        "Valio Oy",
        "10400",
        "ST",
        "ST",
        1.0,
        1.56,
        avail=5000.0,
        rel=0.98,
        co2=1.15,
        vol_l=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "463335",
        "Arla laktoositon maitojuoma 1 l",
        "Arla lactose free milk drink 1l",
        "Arla",
        "Arla Oy",
        "10400",
        "ST",
        "ST",
        1.0,
        1.48,
        avail=5000.0,
        rel=0.97,
        co2=1.18,
        vol_l=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "462775",
        "Arla laktoositon rasvaton maitojuoma 1 l UHT",
        "Arla Milkdrink 1L lactose free fat free UHT",
        "Arla",
        "Arla Oy",
        "10400",
        "ST",
        "ST",
        1.0,
        1.52,
        avail=5000.0,
        rel=0.96,
        co2=1.16,
        vol_l=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "460396",
        "Valio rasvaton maito 10 l novobox",
        "Valio skimmed milk 10 l novobox",
        "Valio",
        "Valio Oy",
        "10400",
        "CS",
        "LTR",
        10.0,
        11.80,
        avail=5000.0,
        rel=0.99,
        co2=1.05,
        vol_l=1.0,
        allergens=("AM", "ML"),
    ),
    _prod(
        "461578",
        "Vaasan ruispalat ruisleipä leipä 45x85 g",
        "Vaasan Rye bread 45x85g",
        "Vaasan",
        "Vaasan Oy",
        "16100",
        "CS",
        "ST",
        45.0,
        14.50,
        avail=5000.0,
        rel=0.97,
        co2=0.95,
        wt_g=85.0,
        allergens=("NR", "AW"),
        claims=("FREE_FROM_LACTOSE",),
        temp=8,
    ),
    _prod(
        "461765",
        "Moilas gluteeniton tumma leipä 600 g",
        "Moilas Gluten-free Dark Bread 600g sliced frozen",
        "Moilas",
        "Moilas Oy",
        "16100",
        "KI",
        "ST",
        6.0,
        18.90,
        avail=5000.0,
        rel=0.96,
        co2=1.20,
        wt_g=600.0,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
        temp=1,
    ),
    _prod(
        "460573",
        "Pesty peruna Bellarosa 1 kg",
        "PP POTATO PRE-WASHED BELLAROSA",
        "PP",
        "Pohjolan Peruna Oy",
        "12100",
        "KG",
        "KG",
        1.0,
        1.15,
        avail=5000.0,
        rel=0.98,
        co2=0.25,
        wt_kg=1.0,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
    ),
    _prod(
        "460200",
        "Elovena täysjyvä kaurahiutale 1 kg",
        "Elovena 1kg wholegrain oat flakes",
        "Elovena",
        "Raisio Oyj",
        "14100",
        "ST",
        "ST",
        1.0,
        2.40,
        avail=5000.0,
        rel=0.99,
        co2=0.80,
        wt_kg=1.0,
        allergens=("GO", "AW"),
        temp=8,
    ),
    _prod(
        "480001",
        "Meira maapähkinä suolapähkinä 1 kg",
        "Meira salted peanuts 1 kg",
        "Meira",
        "Meira Oy",
        "18100",
        "ST",
        "ST",
        1.0,
        6.50,
        avail=5000.0,
        rel=0.96,
        co2=2.10,
        wt_kg=1.0,
        allergens=("AP",),
        may_allergens=("AN",),
        temp=8,
    ),
    _prod(
        "463982",
        "Kieku lattiakananmuna L180 12300g",
        "Kieku Barn Eggs L180 12300g",
        "Kieku",
        "Kieku Oy",
        "10500",
        "KI",
        "KG",
        12.3,
        34.50,
        avail=5000.0,
        rel=0.98,
        co2=2.60,
        wt_kg=1.0,
        allergens=("AE",),
    ),
    _prod(
        "460468",
        "Paulig Presidentti Tumma Paahto kahvi 18x300 g",
        "Paulig Presidentti Tumma Paahto 18x300g",
        "Paulig",
        "Oy Gustav Paulig Ab",
        "18100",
        "KI",
        "ST",
        18.0,
        89.00,
        avail=5000.0,
        rel=0.99,
        co2=5.80,
        wt_g=300.0,
        temp=8,
    ),
    _prod(
        "460884",
        "Reuter Stolt croissant 80x90 g",
        "Reuter & Stolt Croissant butter 18 % 80 x 90g",
        "Reuter & Stolt",
        "Reuter & Stolt",
        "16100",
        "KI",
        "ST",
        80.0,
        46.00,
        avail=5000.0,
        rel=0.95,
        co2=2.90,
        wt_g=90.0,
        allergens=("UW", "AW", "AM", "AE"),
        temp=1,
    ),
    _prod(
        "465101",
        "Valio voi laktoositon 500 g",
        "Valio butter lactose free 500 g",
        "Valio",
        "Valio Oy",
        "10440",
        "ST",
        "ST",
        1.0,
        3.80,
        avail=5000.0,
        rel=0.99,
        co2=8.50,
        wt_g=500.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "465102",
        "Valio maitorahka rahka laktoositon 1 kg",
        "Valio quark lactose free 1 kg",
        "Valio",
        "Valio Oy",
        "10420",
        "ST",
        "ST",
        1.0,
        3.40,
        avail=5000.0,
        rel=0.98,
        co2=2.40,
        wt_kg=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "465103",
        "Valio maustamaton jogurtti laktoositon 1 kg",
        "Valio plain yoghurt lactose free 1 kg",
        "Valio",
        "Valio Oy",
        "10420",
        "ST",
        "ST",
        1.0,
        2.50,
        avail=5000.0,
        rel=0.98,
        co2=1.60,
        wt_kg=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "465104",
        "Valio raejuusto laktoositon 1 kg",
        "Valio cottage cheese lactose free 1 kg",
        "Valio",
        "Valio Oy",
        "10440",
        "ST",
        "ST",
        1.0,
        4.50,
        avail=5000.0,
        rel=0.98,
        co2=3.90,
        wt_kg=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "465105",
        "Valio ruokakerma laktoositon 1 l",
        "Valio cooking cream lactose free 1 l",
        "Valio",
        "Valio Oy",
        "10420",
        "ST",
        "ST",
        1.0,
        3.20,
        avail=5000.0,
        rel=0.99,
        co2=2.80,
        vol_l=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "465106",
        "Valio juustoraaste juusto laktoositon 1 kg",
        "Valio grated cheese lactose free 1 kg",
        "Valio",
        "Valio Oy",
        "10440",
        "ST",
        "ST",
        1.0,
        7.90,
        avail=5000.0,
        rel=0.99,
        co2=6.20,
        wt_kg=1.0,
        allergens=("AM",),
        claims=("FREE_FROM_LACTOSE",),
    ),
    _prod(
        "470001",
        "Myllyn Paras lasagne 3 kg",
        "Myllyn Paras lasagne sheets 3 kg",
        "Myllyn Paras",
        "Myllyn Paras Oy",
        "14100",
        "ST",
        "ST",
        1.0,
        6.90,
        avail=5000.0,
        rel=0.98,
        co2=1.10,
        wt_kg=3.0,
        allergens=("UW", "AW"),
        temp=8,
    ),
    _prod(
        "470002",
        "Barilla pasta 1 kg",
        "Barilla spaghetti pasta 1 kg",
        "Barilla",
        "Barilla",
        "14100",
        "ST",
        "ST",
        1.0,
        2.10,
        avail=10.0,
        rel=0.97,
        co2=1.15,
        wt_kg=1.0,
        allergens=("UW", "AW"),
        temp=8,
    ),
    _prod(
        "470003",
        "Myllyn Paras täysjyvä penne makaroni pasta 1 kg",
        "Myllyn Paras wholegrain penne pasta 1 kg",
        "Myllyn Paras",
        "Myllyn Paras Oy",
        "14100",
        "ST",
        "ST",
        1.0,
        2.35,
        avail=5000.0,
        rel=0.98,
        co2=1.08,
        wt_kg=1.0,
        allergens=("UW", "AW"),
        temp=8,
    ),
    _prod(
        "470004",
        "Atria naudan jauheliha 2 kg",
        "Atria ground beef 2 kg",
        "Atria",
        "Atria Suomi Oy",
        "11100",
        "ST",
        "ST",
        1.0,
        17.80,
        avail=5000.0,
        rel=0.98,
        co2=14.20,
        wt_kg=2.0,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
    ),
    _prod(
        "470005",
        "Mutti murskattu tomaatti 2,5 kg",
        "Mutti crushed tomatoes 2.5 kg",
        "Mutti",
        "Mutti",
        "12100",
        "ST",
        "ST",
        1.0,
        5.40,
        avail=5000.0,
        rel=0.99,
        co2=0.85,
        wt_kg=2.5,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
        temp=8,
    ),
    _prod(
        "470006",
        "Myllyn Paras vehnäjauho 2 kg",
        "Myllyn Paras wheat flour 2 kg",
        "Myllyn Paras",
        "Myllyn Paras Oy",
        "14100",
        "ST",
        "ST",
        1.0,
        2.20,
        avail=5000.0,
        rel=0.99,
        co2=0.70,
        wt_kg=2.0,
        allergens=("UW", "AW"),
        temp=8,
    ),
    _prod(
        "470007",
        "Suomalainen sipuli 1 kg",
        "Finnish onion 1 kg",
        "Suomalainen",
        "Kasvistukku Oy",
        "12100",
        "ST",
        "ST",
        1.0,
        1.40,
        avail=5000.0,
        rel=0.98,
        co2=0.30,
        wt_kg=1.0,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
    ),
    _prod(
        "470008",
        "HK pekoni 1 kg",
        "HK bacon 1 kg",
        "HK",
        "HKFoods Finland Oy",
        "11100",
        "ST",
        "ST",
        1.0,
        8.90,
        avail=5000.0,
        rel=0.97,
        co2=5.40,
        wt_kg=1.0,
        claims=("FREE_FROM_GLUTEN", "FREE_FROM_LACTOSE"),
    ),
    _prod(
        "470009",
        "Tuore kananmuna 1 kg",
        "Fresh eggs 1 kg",
        "Kieku",
        "Kieku Oy",
        "10500",
        "ST",
        "ST",
        1.0,
        4.20,
        avail=5000.0,
        rel=0.99,
        co2=2.50,
        wt_kg=1.0,
        allergens=("AE",),
    ),
]

SPECS_BY_ID = {s["synkka"]["id"]: s for s in CATALOG_SPECS}


def write_mini_data(data_dir: Path) -> None:
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "valio_aimo_junction26_product_data.json").write_text(
        json.dumps([s["synkka"] for s in CATALOG_SPECS], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with open(d / "valio_aimo_junction26_delivery_reliability.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["productId", "deliveryReliability"])
        for s in CATALOG_SPECS:
            w.writerow([s["synkka"]["id"], s["rel"]])
    with open(d / "valio_aimo_junction26_carbon_footprint.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["productId", "AVG kg CO2-eq/kg"])
        for s in CATALOG_SPECS:
            w.writerow([s["synkka"]["id"], s["co2"]])


def _price_payload(body: dict) -> dict:
    customer = body.get("customer", "hotel-aurora")
    req_date = body.get("requestedDate", date.today().isoformat())
    out = []
    for pid in body.get("products", []):
        spec = SPECS_BY_ID.get(str(pid))
        if not spec:
            continue
        net = spec["price"]
        gross = round(net * 1.14, 2)
        out.append(
            {
                "productId": str(pid),
                "quantity": 1.0,
                "catalogPrice": {
                    "value": net,
                    "valueWithTax": gross,
                    "validFrom": "2026-01-01T00:00:00+02:00",
                    "validTo": "2027-12-31T00:00:00+02:00",
                    "currency": "EUR",
                },
                "priceWithTax": {
                    "value": gross,
                    "validFrom": "2026-01-01T00:00:00+02:00",
                    "validTo": "2027-12-31T00:00:00+02:00",
                    "currency": "EUR",
                },
                "comparisonPrice": {
                    "value": net,
                    "valueWithTax": gross,
                    "validFrom": "2026-01-01T00:00:00+02:00",
                    "validTo": "2027-12-31T00:00:00+02:00",
                    "currency": "EUR",
                },
                "tax": {"taxRate": 14.0, "taxValue": round(gross - net, 2)},
                "netPriceFound": True,
                "error": False,
            }
        )
    return {
        "customerId": customer,
        "currency": "EUR",
        "pricingDate": f"{req_date}T00:00:00+02:00",
        "products": out,
    }


def _availability_payload(body: dict) -> dict:
    req_date = body.get("requestedDate", date.today().isoformat())
    prev_day = (date.fromisoformat(req_date[:10]) - timedelta(days=1)).isoformat()
    out = []
    for pid in body.get("products", []):
        spec = SPECS_BY_ID.get(str(pid))
        if not spec:
            continue
        out.append(
            {
                "productId": str(pid),
                "closingTime": f"{prev_day}T17:00:00+02:00",
                "salesBlock": False,
                "logisticModel": "Stock",
                "scheduleLines": [
                    {
                        "salesBlock": False,
                        "availableAmount": spec["avail"],
                        "availableDate": req_date[:10],
                        "salesUnit": spec["sales_unit"],
                    }
                ],
            }
        )
    return {
        "timeNow": f"{date.today().isoformat()}T09:00:00+02:00",
        "requestedDate": f"{req_date[:10]}T00:00:00+02:00",
        "products": out,
    }


class _ValioHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.rstrip("/")
        if path.endswith("/price"):
            payload = _price_payload(body)
        elif path.endswith("/availability"):
            payload = _availability_payload(body)
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_mock_valio_server() -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ValioHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", server
