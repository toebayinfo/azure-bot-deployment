import sys
import traceback
from datetime import datetime
from http import HTTPStatus
import os
import json
import uuid
import asyncio
import aiohttp
import re  # For parsing commands more effectively
import shlex  # To properly handle splitting with quotes
from dotenv import load_dotenv
from aiohttp import web
from aiohttp.web import Request, Response, json_response
from botbuilder.core import TurnContext
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ActivityTypes

# Load environment variables from .env file
load_dotenv()

# Accessing variables from environment
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
INGRAM_CLIENT_ID = os.getenv("INGRAM_CLIENT_ID")
INGRAM_CLIENT_SECRET = os.getenv("INGRAM_CLIENT_SECRET")
INGRAM_CUSTOMER_NUMBER = os.getenv("INGRAM_CUSTOMER_NUMBER")

from bots import EchoBot
from config import DefaultConfig

CONFIG = DefaultConfig()

# Create adapter.
ADAPTER = CloudAdapter(ConfigurationBotFrameworkAuthentication(CONFIG))

# Catch-all for errors.
async def on_error(context: TurnContext, error: Exception):
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    await context.send_activity("The bot encountered an error or bug.")
    await context.send_activity(
        "To continue to run this bot, please fix the bot source code."
    )
    if context.activity.channel_id == "emulator":
        trace_activity = Activity(
            label="TurnError",
            name="on_turn_error Trace",
            timestamp=datetime.utcnow(),
            type=ActivityTypes.trace,
            value=f"{error}",
            value_type="https://www.botframework.com/schemas/error",
        )
        await context.send_activity(trace_activity)

ADAPTER.on_turn_error = on_error

# Create the Bot
class CustomEchoBot(EchoBot):
    def __init__(self):
        self.access_token, self.token_expire_time = asyncio.run(self.get_access_token())

    async def ensure_access_token(self):
        if not self.access_token or asyncio.get_running_loop().time() > self.token_expire_time:
            self.access_token, self.token_expire_time = await self.get_access_token()
            if not self.access_token:
                raise Exception("Unable to retrieve a valid token")

    async def get_access_token(self):
        url = "https://api.ingrammicro.com:443/oauth/oauth30/token"
        payload = {
            'grant_type': 'client_credentials',
            'client_id': INGRAM_CLIENT_ID,
            'client_secret': INGRAM_CLIENT_SECRET
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    expire_time = asyncio.get_running_loop().time() + int(data['expires_in']) - 300
                    return data['access_token'], expire_time
                else:
                    print(f"Failed to obtain access token: {response.status}, {await response.text()}")
                    return None, None

    async def on_turn(self, turn_context: TurnContext):
        await self.ensure_access_token()
        if turn_context.activity.type == 'message':
            user_message = turn_context.activity.text.lower()
            keyword_search = re.search(r"search product details for (.+)", user_message)
            product_id_search = re.search(r"price and availability for (\w+)", user_message)

            if keyword_search:
                keywords = keyword_search.group(1).split(',')
                products_data = await self.fetch_products(self.access_token, keywords)
                response = self.format_response(products_data)
                await turn_context.send_activity(Activity(type="message", text=response))
            elif product_id_search:
                product_id = product_id_search.group(1)
                response = await self.fetch_price_and_availability(product_id)
                await turn_context.send_activity(Activity(type="message", text=response))
            else:
                response = await self.ask_openai(user_message)
                await turn_context.send_activity(Activity(type="message", text=response))

        elif turn_context.activity.type == 'conversationUpdate':
            if turn_context.activity.members_added:
                for member in turn_context.activity.members_added:
                    if member.id != turn_context.activity.recipient.id:
                        await turn_context.send_activity(Activity(type="message", text="Welcome to the Ingram Micro Bot! Type 'hello' to start or ask me anything."))

    async def fetch_products(self, access_token, keywords):
        results = []
        url = 'https://api.ingrammicro.com:443/sandbox/resellers/v6/catalog'
        headers = {
            'Authorization': f'Bearer {access_token}',
            'IM-CustomerNumber': INGRAM_CUSTOMER_NUMBER,
            'IM-SenderID': 'MyCompany',
            'IM-CorrelationID': str(uuid.uuid4()),
            'IM-CountryCode': 'US',
            'Accept-Language': 'en',
            'Content-Type': 'application/json',
        }

        async with aiohttp.ClientSession() as session:
            for keyword in keywords:
                params = {
                    'pageNumber': 1,
                    'pageSize': 25,
                    'type': 'IM::any',
                    'keyword': keyword.strip(),
                    'includeProductAttributes': 'true',
                    'includePricing': 'true',
                    'includeAvailability': 'true'
                }
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        results.append(data)
                    else:
                        print(f"Failed API Call for keyword '{keyword}': {response.status}, {await response.text()}")
        return results

    async def fetch_price_and_availability(self, ingram_part_number):
        url = (f'https://api.ingrammicro.com:443/sandbox/resellers/v6/catalog/priceandavailability'
            f'?includePricing=true&includeAvailability=true&includeProductAttributes=true')

        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
            'IM-CustomerNumber': INGRAM_CUSTOMER_NUMBER,
            'IM-CountryCode': 'US',
            'IM-CorrelationID': str(uuid.uuid4()),
            'IM-SenderID': 'MyCompany',
            'Accept': 'application/json'
        }

        data = json.dumps({"products": [{"ingramPartNumber": ingram_part_number.upper()}]})

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 200:
                    product_details = await response.json()
                    return self.format_product_details(product_details)
                else:
                    error_message = await response.text()
                    print(f"Failed to fetch details: {response.status} - {error_message}")
                    return f"Failed to fetch details: {response.status} - {error_message}"

    async def ask_openai(self, prompt):
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {
            "model": "gpt-4-turbo",
            "messages": [{"role": "user", "content": prompt}]
        }
        url = "https://api.openai.com/v1/chat/completions"
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['choices'][0]['message']['content'].strip()
                else:
                    print("Failed to process request with OpenAI:", response.status, await response.text())
                    return "I had an error processing your request. Please try again later."

    def format_response(self, products):
        formatted_products = []
        for product_data in products:
            for product in product_data.get('catalog', []):
                links_info = "No direct link available"
                if 'links' in product and product['links']:
                    link = next((link for link in product['links'] if link.get('type') == 'GET'), None)
                    links_info = link['href'] if link else links_info
                description = product.get('description', 'No description available')
                category = product.get('category', 'No category')
                vendor_name = product.get('vendorName', 'No vendor name')
                vendorPartNumber= product.get('vendorPartNumber', 'No vendor Part number')
                extraDescription = product.get('extraDescription', 'No Extended Description available')
                subCategory = product.get('subCategory', 'No subcategory')
                productType = product.get('productType', 'No product type')
                formatted_product = f"{vendor_name} - {description} - {category} - {subCategory} - {productType}\nPrice and availability: {links_info}"
                formatted_products.append(formatted_product)
        return "\n\n".join(formatted_products)

    def format_product_details(self, product_details):
        formatted_products = []
        for product in product_details:
            ingram_part_number = product.get('ingramPartNumber', 'N/A').upper()
            description = product.get('description', 'No description available')
            product_status_code = product.get('productStatusCode', 'N/A')
            product_status_message = product.get('productStatusMessage', 'No status message available')

            availability = product.get('availability', {})
            available = availability.get('available', False)
            total_availability = availability.get('totalAvailability', 0)

            pricing = product.get('pricing', {})
            retail_price = pricing.get('retailPrice', 'N/A')
            customer_price = pricing.get('customerPrice', 'N/A')

            formatted_product = (
                f"Product Number: {ingram_part_number} \n\n "
                f"Product Status Code: {product_status_code} - \n\n {product_status_message} \n\n "
                f"Description: {description} \n\n "
                f"Availability: {'Available' if available else 'Not Available'} \n\n "
                f"Total Availability: {total_availability} \n\n "
                f"Retail Price: {retail_price} \n\n "
                f"Customer Price: {customer_price}"
            )
            formatted_products.append(formatted_product)
        
        return "\n\n".join(formatted_products)

BOT = CustomEchoBot()

# Listen for incoming requests on /api/messages
async def messages(req: Request) -> Response:
    if "application/json" in req.headers["Content-Type"]:
        body = await req.json()
    else:
        return Response(status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    activity = Activity().deserialize(body)
    auth_header = req.headers["Authorization"] if "Authorization" in req.headers else ""

    response = await ADAPTER.process_activity(auth_header, activity, BOT.on_turn)
    if response:
        return json_response(data=response.body, status=response.status)
    return Response(status=HTTPStatus.OK)

# Health check endpoint
async def health_check(req: Request) -> Response:
    return Response(status=HTTPStatus.OK)

APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/health", health_check)  # Add health check endpoint

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 8000))
        web.run_app(APP, host="0.0.0.0", port=port)
    except Exception as error:
        raise error
