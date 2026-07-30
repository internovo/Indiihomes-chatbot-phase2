import asyncio
import json
import traceback

import httpx

from config import get_settings


async def main():
    settings = get_settings()

    base_url = settings.wati_endpoint.rstrip("/")
    api_key = settings.wati_api_key

    url = f"{base_url}/api/v1/sendTemplateMessage"

    # Test phone number
    phone = "919999999999"

    # Test data (replace with your own if needed)
    customer_name = "Rahul"
    project_name = "38 Avenue"

    payload = {
        "template_name": "campaign_property_intro",
        "broadcast_name": "campaign_property_intro",
        "parameters": [
            {
                "name": "1",
                "value": customer_name,
            },
            {
                "name": "2",
                "value": project_name,
            },
        ],
    }

    print("=" * 80)
    print("WATI TEMPLATE TEST")
    print("=" * 80)
    print("Endpoint :", url)
    print("Phone    :", phone)
    print("API Key  :", "*" * max(len(api_key) - 4, 0) + api_key[-4:])
    print()

    print("Payload:")
    print(json.dumps(payload, indent=4))

    print("=" * 80)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                params={
                    "whatsappNumber": phone,
                },
                headers=headers,
                json=payload,
            )

            print("\nStatus Code:")
            print(response.status_code)

            print("\nResponse Headers:")
            print(dict(response.headers))

            print("\nResponse Body:")
            try:
                print(json.dumps(response.json(), indent=4))
            except Exception:
                print(response.text)

            if response.status_code == 200:
                print("\n✅ Template sent successfully!")
            else:
                print(f"\n❌ Request failed ({response.status_code})")

    except Exception:
        print("\nUnexpected Exception:")
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())