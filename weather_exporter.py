#!/usr/bin/env python3
"""
Prometheus exporter for SMHI weather data
Exports current weather metrics for Stockholm/Danderyd area
"""

import time
import sys
import requests
from prometheus_client import start_http_server, Gauge
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration - Danderyd coordinates
LATITUDE = 59.4164
LONGITUDE = 18.0131
SMHI_API_URL = f"https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/geotype/point/lon/{LONGITUDE}/lat/{LATITUDE}/data.json"
SCRAPE_INTERVAL = 300  # 5 minutes (weather doesn't change that fast)
MAX_CONSECUTIVE_FAILURES = 3  # Exit after this many consecutive fetch failures

# Prometheus metrics
weather_temperature = Gauge(
    'weather_temperature_celsius',
    'Current temperature in Celsius',
    ['location']
)

weather_wind_speed = Gauge(
    'weather_wind_speed_mps',
    'Wind speed in meters per second',
    ['location']
)

weather_wind_gust = Gauge(
    'weather_wind_gust_mps',
    'Wind gust speed in meters per second',
    ['location']
)

weather_wind_direction = Gauge(
    'weather_wind_direction_degrees',
    'Wind direction in degrees',
    ['location']
)

weather_humidity = Gauge(
    'weather_humidity_percent',
    'Relative humidity in percent',
    ['location']
)

weather_precipitation = Gauge(
    'weather_precipitation_mm',
    'Precipitation intensity in mm/h',
    ['location']
)

weather_cloud_cover = Gauge(
    'weather_cloud_cover_percent',
    'Total cloud cover in percent (0-100 scale from octas)',
    ['location']
)

weather_visibility = Gauge(
    'weather_visibility_km',
    'Horizontal visibility in kilometers',
    ['location']
)

weather_pressure = Gauge(
    'weather_pressure_hpa',
    'Air pressure in hPa',
    ['location']
)

weather_thunder_probability = Gauge(
    'weather_thunder_probability_percent',
    'Thunder probability in percent',
    ['location']
)

weather_symbol = Gauge(
    'weather_symbol_code',
    'SMHI weather symbol code (1-27)',
    ['location']
)

exporter_scrape_duration = Gauge(
    'weather_exporter_scrape_duration_seconds',
    'Time spent scraping weather data'
)

exporter_last_success = Gauge(
    'weather_exporter_last_success_timestamp',
    'Timestamp of last successful scrape'
)

# SMHI snow1g parameter mapping (flat data dict)
PARAM_MAP = {
    'air_temperature': ('temperature', weather_temperature),
    'wind_speed': ('wind_speed', weather_wind_speed),
    'wind_speed_of_gust': ('wind_gust', weather_wind_gust),
    'wind_from_direction': ('wind_direction', weather_wind_direction),
    'relative_humidity': ('humidity', weather_humidity),
    'precipitation_amount_mean': ('precipitation', weather_precipitation),
    'cloud_area_fraction': ('cloud_cover', weather_cloud_cover),  # Already 0-100%
    'visibility_in_air': ('visibility', weather_visibility),
    'air_pressure_at_mean_sea_level': ('pressure', weather_pressure),
    'thunderstorm_probability': ('thunder', weather_thunder_probability),
    'symbol_code': ('symbol', weather_symbol),
}


def fetch_weather() -> tuple[dict | None, list]:
    """Fetch current weather and forecast from SMHI API (snow1g)."""
    try:
        response = requests.get(SMHI_API_URL, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # Get the first (current) time series entry
        if not data.get('timeSeries'):
            logger.warning("No timeSeries in SMHI response")
            return None, []
        
        current = data['timeSeries'][0]
        params = current.get('data', {})
        
        # Get upcoming precipitation forecast (next 6 hours)
        precipitation_forecast = []
        for entry in data['timeSeries'][1:7]:  # Next 6 hours
            entry_data = entry.get('data', {})
            precipitation_forecast.append({
                'time': entry.get('time', ''),
                'precipitation': entry_data.get('precipitation_amount_mean', 0),
                'symbol': entry_data.get('symbol_code', 1)
            })
        
        return params, precipitation_forecast
        
    except Exception as e:
        logger.error(f"Error fetching weather: {e}")
        return None, []


def update_metrics():
    """Fetch data and update Prometheus metrics."""
    global latest_weather
    start_time = time.time()
    location = 'Danderyd'
    
    params, precip_forecast = fetch_weather()
    
    if params:
        weather_data = {}
        for smhi_param, (name, gauge) in PARAM_MAP.items():
            if smhi_param in params:
                value = params[smhi_param]
                
                # cloud_area_fraction is already 0-100% in snow1g
                
                gauge.labels(location=location).set(value)
                weather_data[name] = value
                logger.debug(f"Set {name} = {value}")
        
        # Calculate "feels like" temperature (wind chill)
        temp = weather_data.get('temperature', 0)
        wind = weather_data.get('wind_speed', 0)
        if temp <= 10 and wind > 0:
            # Wind chill formula (metric)
            feels_like = 13.12 + 0.6215 * temp - 11.37 * (wind * 3.6) ** 0.16 + 0.3965 * temp * (wind * 3.6) ** 0.16
        else:
            feels_like = temp
        weather_data['feels_like'] = round(feels_like, 1)
        
        # Summarize upcoming precipitation
        total_precip = sum(p['precipitation'] for p in precip_forecast)
        max_precip = max((p['precipitation'] for p in precip_forecast), default=0)
        # Find when precipitation starts (if any)
        precip_starts = None
        for i, p in enumerate(precip_forecast):
            if p['precipitation'] > 0:
                precip_starts = i + 1  # hours from now
                break
        
        weather_data['precip_6h'] = round(total_precip, 1)
        weather_data['precip_max'] = round(max_precip, 1)
        weather_data['precip_starts_in'] = precip_starts  # None if no precip expected
        
        # Store for JSON endpoint
        latest_weather = weather_data
        
        exporter_last_success.set(time.time())
        logger.info(f"Weather update: {params.get('air_temperature', '?')}°C, "
                   f"wind {params.get('wind_speed', '?')} m/s, "
                   f"humidity {params.get('relative_humidity', '?')}%")
    else:
        logger.warning("Failed to fetch weather data")
    
    duration = time.time() - start_time
    exporter_scrape_duration.set(duration)


from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

# Store latest weather for JSON endpoint
latest_weather = {}

class JSONHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/json':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            # Wrap in array for Grafana Infinity datasource compatibility
            self.wfile.write(json.dumps([latest_weather] if latest_weather else []).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass


def run_json_server():
    server = ThreadingHTTPServer(('0.0.0.0', 9104), JSONHandler)
    server.serve_forever()


def main():
    """Main entry point."""
    port = 9102
    logger.info(f"Starting weather exporter on port {port} (metrics) and 9104 (JSON)")
    start_http_server(port)
    
    # Start JSON server in background thread
    json_thread = threading.Thread(target=run_json_server, daemon=True)
    json_thread.start()
    
    consecutive_failures = 0
    while True:
        try:
            update_metrics()
            if latest_weather:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                logger.warning(f"Fetch failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.critical(f"{MAX_CONSECUTIVE_FAILURES} consecutive failures, exiting")
                    sys.exit(1)
        except Exception as e:
            logger.error(f"Error updating metrics: {e}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(f"{MAX_CONSECUTIVE_FAILURES} consecutive failures, exiting")
                sys.exit(1)
        
        time.sleep(SCRAPE_INTERVAL)


if __name__ == '__main__':
    main()
