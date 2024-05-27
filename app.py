import sys
import traceback
from datetime import datetime
from http import HTTPStatus
import os
import json
import uuid
import asyncio
import aiohttp
import re
import shlex  
from dotenv import load_dotenv
from aiohttp import web
from aiohttp.web import Request, Response, json_response
from botbuilder.core import TurnContext, MemoryStorage, UserState
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

# Create memory storage and user state
MEMORY_STORAGE = MemoryStorage()
USER_STATE = UserState(MEMORY_STORAGE)

# Catch-all for errors.
async def on_error(context: TurnContext, error: Exception):
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    await context.send_activity("The bot encountered an error or bug.")
    await context.send_activity("To continue to run this bot, please fix the bot source code.")
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
        super().__init__()
        self.access_token, self.token_expire_time = asyncio.run(self.get_access_token())
        self.user_state_accessor = USER_STATE.create_property("UserPageState")

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
        user_page_state = await self.user_state_accessor.get(turn_context, {"current_page": 1, "keywords": []})

        if turn_context.activity.type == 'message':
            user_message = turn_context.activity.text.lower()
            keyword_search = re.search(r"search product details for (.+)", user_message)
            page_search = re.search(r"page (\d+)", user_message)
            product_id_search = re.search(r"price and availability for (\w+)", user_message)

            if keyword_search:
                keywords = keyword_search.group(1).split(',')
                category = await self.classify_query(keywords[0])
                user_page_state['keywords'] = keywords
                user_page_state['current_page'] = 1
                products_data = await self.fetch_products(self.access_token, keywords, category, user_page_state['current_page'])
                response = self.format_response(products_data, user_page_state['current_page'])
                await turn_context.send_activity(Activity(type="message", text=response))
            elif page_search:
                page_number = int(page_search.group(1))
                user_page_state['current_page'] = page_number
                keywords = user_page_state.get('keywords', [])
                category = await self.classify_query(keywords[0])
                products_data = await self.fetch_products(self.access_token, keywords, category, page_number)
                response = self.format_response(products_data, page_number)
                await turn_context.send_activity(Activity(type="message", text=response))
            elif product_id_search:
                product_id = product_id_search.group(1)
                response = await self.fetch_price_and_availability(product_id)
                await turn_context.send_activity(Activity(type="message", text=response))
            else:
                response = await self.ask_openai(user_message)
                await turn_context.send_activity(Activity(type="message", text=response))

            await self.user_state_accessor.set(turn_context, user_page_state)
            await USER_STATE.save_changes(turn_context)
        elif turn_context.activity.type == 'conversationUpdate':
            if turn_context.activity.members_added:
                for member in turn_context.activity.members_added:
                    if member.id != turn_context.activity.recipient.id:
                        await turn_context.send_activity(Activity(type="message", text="Welcome to the Apollo Bot! How can I help you today?"))

    async def classify_query(self, query):
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {
            "model": "gpt-4-turbo",
            "messages": [
                {"role": "system", "content": "Classify the following query into one of these categories: Computer Systems, Accessories, Network Devices, Other. Examples: 'hp laptop' -> Computer Systems, 'dell laptop battery' -> Accessories, 'ubiquiti unifi' -> Network Devices"},
                {"role": "user", "content": query}
            ]
        }
        url = "https://api.openai.com/v1/chat/completions"

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    classification = data['choices'][0]['message']['content'].strip()
                    return classification
                else:
                    print("Failed to classify query with OpenAI:", response.status, await response.text())
                    return "Other"

    async def fetch_products(self, access_token, keywords, category, page_number):
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
                    'pageNumber': page_number,
                    'pageSize': 10,  # Adjust page size as needed
                    'type': 'IM::any',
                    'keyword': keyword.strip(),
                    'includeProductAttributes': 'true',
                    'includePricing': 'true',
                    'includeAvailability': 'true',
                    'skipAuthorisation': 'true'
                }

                # Add the category filter based on classification
                if category in ['Computer Systems', 'Accessories', 'Network Devices']:
                    params['category'] = category

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
            "messages": [
                {"role": "system", "content": "Provide clear and straightforward answers. Do not mention the model's last update, or similar phrases."},
                {"role": "user", "content": prompt}
            ]
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

    def format_response(self, products, current_page):
        formatted_products = []
        items_in_current_page = 0
        product_index = 1  # Initialize product index

        for product_data in products:
            items_in_current_page += len(product_data.get('catalog', []))
            for product in product_data.get('catalog', []):
                links_info = "No direct link available"
                if 'links' in product and product['links']:
                    link = next((link for link in product['links'] if link.get('type') == 'GET'), None)
                    links_info = link['href'] if link else links_info
                description = product.get('description', 'No description available')
                category = product.get('category', 'No category')
                vendor_name = product.get('vendorName', 'No vendor name')
                vendorPartNumber = product.get('vendorPartNumber', 'No vendor Part number')
                extraDescription = product.get('extraDescription', 'No Extended Description available')
                subCategory = product.get('subCategory', 'No subcategory')
                productType = product.get('productType', 'No product type')
                formatted_product = f"**Product Details:** {vendor_name} - {description}  \n **Category:** {category} - {subCategory}  \n **Prodyct Type:** {productType}  \n**Price and availability:** {links_info}"
                formatted_products.append(formatted_product)
        response_text = "\n\n".join(formatted_products)
        print("Formatted response:\n", response_text)  # Debug statement

        # Check if there are more items
        items_per_page = 10  # This should match the 'pageSize' parameter in your fetch_products method
        if items_in_current_page == items_per_page:
            response_text += f"\n\nYou are currently on page {current_page}. There are more results available."
            response_text += f"\nEnter 'page <number>' to navigate to a specific page."
            response_text += f"\nFor example, type 'page {current_page + 1}' to view the next page."
        else:
            response_text += f"\n\nYou are on the last page ({current_page})."

        return response_text

    def format_product_details(self, product_details):
        formatted_products = []
        if isinstance(product_details, dict):
            product_details = product_details.get('products', [])
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
                f"Product Number: {ingram_part_number}\n"
                f"Product Status Code: {product_status_code}\n"
                f"Product Status Message: {product_status_message}\n"
                f"Description: {description}\n"
                f"Availability: {'Available' if available else 'Not Available'}\n"
                f"Total Availability: {total_availability}\n"
                f"Retail Price: {retail_price}\n"
                f"Customer Price: {customer_price}\n"
                "----------------------------------------\n"
            )
            formatted_products.append(formatted_product)

        response_text = "\n".join(formatted_products)
        print("Formatted product details:\n", response_text)  # Debug statement
        return response_text

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
