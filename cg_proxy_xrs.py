import asyncio
import time
import requests
from aiohttp import web
import json
import logging
import sys

# Cache to store the fetched JSON data
data_cache = {"cg_coins_list": {}, "cg_data": {}}
# Configure logging to output to the console
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
# Set the log level for urllib3 to INFO
logging.getLogger('urllib3').setLevel(logging.INFO)
logging.getLogger('aiohttp').setLevel(logging.INFO)


async def fetch_and_parse_json(url, max_retries=5):
    retries = 0
    while retries < max_retries:
        try:
            response = requests.get(url)
            response.raise_for_status()  # Raise an exception for bad status codes
            data = response.json()
            return data
        except Exception as e:
            logging.error(f"Error fetching data")
            retries += 1
            if retries <= max_retries:
                logging.info(f"Retrying in {30 * retries} seconds... (Attempt {retries}/{max_retries})")
                await asyncio.sleep(30 * retries)  # Wait for 20 seconds before retrying
    logging.error(f"Maximum number of retries ({max_retries}) reached. Unable to fetch data.")
    return None


async def update_coingecko_coins_list():
    logging.info("Starting update_coingecko_coins_list task...")
    while True:
        try:
            # Fetch data from the CoinGecko API
            url = "https://api.coingecko.com/api/v3/coins/list"
            data = await fetch_and_parse_json(url)
            timestamp = time.time()
            data_cache["cg_coins_list"] = {"data": data, "timestamp": timestamp}
            logging.info("cg_coins_list data updated")
        except Exception as e:
            logging.error("Error updating cg_coins_list data")
        await asyncio.sleep(60 * 60)


async def update_coingecko_coins_tickers():
    logging.info("Starting update_coingecko_coins_tickers task...")
    final_data = {}  # Initialize an empty dictionary to accumulate all response data
    while True:
        try:
            if data_cache and 'cg_coins_list' in data_cache:
                coin_ids = [entry['id'] for entry in data_cache['cg_coins_list']['data']]
                chunk_start_index = 0
                while chunk_start_index < len(coin_ids):
                    chunk_end_index = chunk_start_index
                    while chunk_end_index < len(coin_ids):
                        chunk_ids = coin_ids[chunk_start_index:chunk_end_index + 1]
                        ids_string = ','.join(chunk_ids)
                        url_template = "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=usd&include_24hr_vol=true"
                        url = url_template.format(ids_string)
                        if len(url) > 8000:
                            # If the URL length exceeds the limit, remove the last coin ID and retest
                            chunk_end_index -= 1
                            break
                        else:
                            chunk_end_index += 1
                    # Make the API call with the constructed ids_string
                    chunk_ids = coin_ids[chunk_start_index:chunk_end_index + 1]
                    ids_string = ','.join(chunk_ids)
                    url = url_template.format(ids_string)
                    data = await fetch_and_parse_json(url)
                    # logging.info(chunk_start_index, chunk_end_index, len(chunk_ids), len(coin_ids))
                    # Update the final_data dictionary with the data from the current API call
                    timestamp = time.time()
                    for key in data:
                        data[key]["timestamp"] = timestamp
                    # logging.info("updated data for :", chunk_ids)
                    data_cache['cg_data'].update(data)
                    # Move to the next chunk
                    chunk_start_index = chunk_end_index + 1
                    await asyncio.sleep(15)
                # logging.info(data_cache['cg_data'])
        except Exception as e:
            logging.error("Error updating cg_coins_tickers data:", e)
            await asyncio.sleep(15)
        else:
            await asyncio.sleep(15)


async def cg_coins_list():
    if data_cache and "cg_coins_list" in data_cache:
        return {"success": True, "reply": data_cache['cg_coins_list']}
    else:
        return {"success": False, "reply": "No data to serve"}


async def cg_coins_data(coins):
    if not isinstance(coins, list) or not all(isinstance(coin, str) for coin in coins):
        return {"success": False, "reply": "Invalid parameters: coins must be a list of strings"}

    result = {}
    for coin in coins:
        coin = coin.lower()
        if coin in data_cache['cg_data']:
            result[coin] = data_cache['cg_data'][coin]
        else:
            result[coin] = {"code": 404, "error": "requested coin not in cache"}
    if result:
        return {"success": True, "reply": result}
    else:
        return {"success": False, "reply": "error gathering requested coin(s) data"}


async def start_server():
    logging.info("Starting JSON-RPC server...")
    app = web.Application()
    app.router.add_post('/', handle_request)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()


async def handle_request(request):
    try:

        request_data = await request.json()
        method_name = request_data.get('method') if 'method' in request_data else None
        params = request_data.get('params') if 'params' in request_data else None

        # Logging the method and params
        ip_address = request.headers.get('X-Forwarded-For') or request.remote
        logging.info(f"Ip: {ip_address}, Method: {method_name}, Params: {params}")

        if method_name:
            if method_name == 'cg_coins_list':
                result = await cg_coins_list()
                return web.json_response(result)
            elif method_name == 'cg_coins_data':
                result = await cg_coins_data(params)
                return web.json_response(result)
            else:
                raise ValueError("Method not found")
        else:
            raise ValueError("Method not provided")
    except Exception as e:
        logging.error(f"Error handling request from {ip_address}: {e}")
        return web.json_response({"success": False, "reply": str(e)}, status=400)


async def main():
    # Start the update_coingecko functions as a separate tasks
    asyncio.create_task(update_coingecko_coins_list())
    asyncio.create_task(update_coingecko_coins_tickers())

    # Start the JSON-RPC server in a dedicated async function
    asyncio.create_task(start_server())


if __name__ == '__main__':
    logging.info("Starting main function...")
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
