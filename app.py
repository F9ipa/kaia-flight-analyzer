from datetime import datetime, timedelta
from collections import Counter
from flask import Flask, render_template, request
import cloudscraper
import os

app = Flask(__name__)

class FlightAnalyzer:
    def __init__(self):
        self.url = "https://www.kaia.sa/ext-api/flightsearch/flights"
        self.headers = {
            "Accept": "application/json",
            "Authorization": "Basic dGVzdGVyOlRoZTMzY3JldA==",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # استخدام cloudscraper لتجاوز حماية Cloudflare
        self.session = cloudscraper.create_session(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True
            }
        )
        self.session.headers.update(self.headers)

    def fetch_data(self, start_iso, end_iso):
        params = {
            "$filter": (
                f"(EarlyOrDelayedDateTime ge {start_iso} "
                f"and EarlyOrDelayedDateTime lt {end_iso}) "
                "and PublicRemark/Code ne 'NOP' "
                "and tolower(FlightNature) eq 'arrival' "
                "and Terminal eq 'T1' "
                "and tolower(InternationalStatus) eq 'international'"
            ),
            "$orderby": "EarlyOrDelayedDateTime",
            "$count": "true"
        }
        try:
            response = self.session.get(self.url, params=params, timeout=30)
            response.raise_for_status()
            return response.json().get("value", [])
        except Exception:
            return None

    def parse_time(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def format_12h(self, dt):
        hour = dt.hour % 12 or 12
        suffix = "ص" if dt.hour < 12 else "م"
        return f"{hour}:{dt.minute:02d} {suffix}"

    def get_flight_code(self, flight):
        if flight.get("FullFlightNumber"):
            return str(flight["FullFlightNumber"])
        airline = flight.get("Airline") or {}
        airline_code = airline.get("IATA", "") if isinstance(airline, dict) else ""
        return f"{airline_code}{flight.get('FlightNumber', '')}"

    def get_events(self, flight):
        events = flight.get("FlightEvents")
        return events if isinstance(events, list) else []

    def is_landed(self, flight):
        for event in self.get_events(flight):
            if isinstance(event, dict) and event.get("EventCode") in ("AA", "de--_ActualArrival") and event.get("DateTime"):
                return True
        return False

    def is_delayed(self, flight):
        sto = self.parse_time(flight.get("STODateTime"))
        estimated = self.parse_time(flight.get("EarlyOrDelayedDateTime"))
        if sto and estimated:
            if (estimated - sto).total_seconds() / 60 > 1:
                return True
        remark = str((flight.get("PublicRemark") or {}).get("Code", "")).upper().strip()
        return remark in {"DEL", "DLY", "DELAYED"}

    def calculate_gaps(self, flights):
        gaps = []
        for first, second in zip(flights, flights[1:]):
            minutes = (second["time"] - first["time"]).total_seconds() / 60
            if minutes >= 15:
                gaps.append({
                    "from": self.format_12h(first["time"]),
                    "to": self.format_12h(second["time"]),
                    "minutes": int(round(minutes)),
                    "from_code": first["code"],
                    "to_code": second["code"]
                })
        return gaps

    def analyze(self, day, start_str, end_str):
        now = datetime.now().astimezone()
        try:
            day_val = int(day) if day else now.day
            start_clock = datetime.strptime(start_str, "%H:%M").time()
            end_clock = datetime.strptime(end_str, "%H:%M").time()

            target_date = now.replace(day=day_val, hour=0, minute=0, second=0, microsecond=0)
            start_dt = target_date.replace(hour=start_clock.hour, minute=start_clock.minute)
            end_dt = target_date.replace(hour=end_clock.hour, minute=end_clock.minute)

            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000+03:00"
            end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000+03:00"
        except Exception:
            return {"error": "خطأ في صيغة التاريخ أو الوقت المدخل."}

        data = self.fetch_data(start_iso, end_iso)
        if data is None:
            return {"error": "حدث خطأ أثناء الاتصال بموقع المطار (قد تكون حماية Cloudflare حظرت الطلب)."}
        if not data:
            return {"error": "لا توجد رحلات مطابقة للفترة المحددة."}

        total = 0
        landed = 0
        delayed = 0
        remaining = 0
        hourly_stats = Counter()
        flights_for_gaps = []
        current_time = datetime.now().astimezone()

        for flight in data:
            flight_time = self.parse_time(flight.get("EarlyOrDelayedDateTime"))
            if not flight_time:
                continue
            flight_time = flight_time.astimezone(current_time.tzinfo)
            code = self.get_flight_code(flight)

            total += 1
            hourly_stats[flight_time.hour] += 1
            flights_for_gaps.append({"time": flight_time, "code": code})

            if self.is_landed(flight):
                landed += 1
                continue
            if self.is_delayed(flight):
                delayed += 1
            if flight_time > current_time:
                remaining += 1

        flights_for_gaps.sort(key=lambda x: x["time"])
        gaps = self.calculate_gaps(flights_for_gaps)

        peak_info = "لا توجد بيانات"
        if hourly_stats:
            peak_hour, peak_count = max(hourly_stats.items(), key=lambda x: x[1])
            peak_start = target_date.replace(hour=peak_hour, minute=0)
            peak_end = peak_start + timedelta(hours=1)
            peak_info = f"{self.format_12h(peak_start)} - {self.format_12h(peak_end)} (عدد الرحلات: {peak_count})"

        largest_gap = max(gaps, key=lambda x: x["minutes"]) if gaps else None

        return {
            "date": target_date.strftime("%Y-%m-%d"),
            "period": f"{self.format_12h(start_dt)} إلى {self.format_12h(end_dt)}",
            "total": total,
            "landed": landed,
            "delayed": delayed,
            "remaining": remaining,
            "peak": peak_info,
            "gaps": gaps,
            "largest_gap": largest_gap
        }

analyzer = FlightAnalyzer()

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    if request.method == "POST":
        day = request.form.get("day")
        start = request.form.get("start", "00:00")
        end = request.form.get("end", "23:59")
        result = analyzer.analyze(day, start, end)
    
    return render_template("index.html", result=result)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
