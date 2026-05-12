"""
Platah — Google Ads MCP Server (HTTP/SSE)
Deploy no Railway como serviço web separado.
"""

import os
import json
import requests
from datetime import date, timedelta

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import Response
import uvicorn

# ─── Credenciais ─────────────────────────────────────────────────────────────

GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
GOOGLE_CLIENT_ID           = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET       = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN       = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

# ─── Contas conhecidas ────────────────────────────────────────────────────────

CUSTOMER_IDS = {
    "oimu":     "7132104797",
    "kukiê":    "4026094180",
    "infanti":  "1383946764",
    "vj":       "2798923099",
    "undertop": "2341810352",
    "787":      "2276201464",
    "monnari":  "4241884415",
    "cora":     "4303131547",
    "mcc":      "1698381003",
}

# ─── Google Ads helpers ───────────────────────────────────────────────────────

def _access_token():
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _query(customer_id, gaql):
    token = _access_token()
    headers = {
        "Authorization":   f"Bearer {token}",
        "developer-token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "Content-Type":    "application/json",
    }
    resp = requests.post(
        f"https://googleads.googleapis.com/v23/customers/{customer_id}/googleAds:search",
        headers=headers,
        json={"query": gaql},
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"Google Ads API {resp.status_code}: {resp.text[:400]}")
    return resp.json().get("results", [])


def _date_range(period: str):
    today     = date.today()
    yesterday = today - timedelta(days=1)
    if period == "this_month":
        return yesterday.replace(day=1).strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")
    if period == "last_7_days":
        return (yesterday - timedelta(days=6)).strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")
    if period == "last_30_days":
        return (yesterday - timedelta(days=29)).strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev  = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    return yesterday.replace(day=1).strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


def _resolve(raw: str) -> str:
    key = raw.strip().lower()
    return CUSTOMER_IDS.get(key, raw.replace("-", "").strip())

# ─── MCP Server ───────────────────────────────────────────────────────────────

server = Server("platah-google-ads")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_campaign_performance",
            description="Retorna performance por campanha de uma conta Google Ads. Aceita nome do cliente (oimu/kukiê/infanti/vj/undertop/787/monnari/cora) ou customer_id numérico.",
            inputSchema={
                "type": "object",
                "properties": {
                    "customer": {"type": "string", "description": "Nome do cliente ou customer_id"},
                    "period":   {"type": "string", "enum": ["this_month", "last_7_days", "last_30_days", "last_month"], "default": "this_month"},
                },
                "required": ["customer"],
            },
        ),
        Tool(
            name="get_account_summary",
            description="Retorna resumo consolidado (gasto, conversões, ROAS) de uma conta Google Ads. Aceita nome do cliente ou customer_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "customer": {"type": "string", "description": "Nome do cliente ou customer_id"},
                    "period":   {"type": "string", "enum": ["this_month", "last_7_days", "last_30_days", "last_month"], "default": "this_month"},
                },
                "required": ["customer"],
            },
        ),
        Tool(
            name="list_customers",
            description="Lista todos os clientes cadastrados com seus customer_ids.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "list_customers":
        result = {k: v for k, v in CUSTOMER_IDS.items() if k != "mcc"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    customer_id = _resolve(arguments.get("customer", ""))
    period      = arguments.get("period", "this_month")
    date_from, date_to = _date_range(period)

    if name == "get_campaign_performance":
        gaql = f"""
            SELECT
                campaign.name,
                campaign.status,
                metrics.cost_micros,
                metrics.conversions_value,
                metrics.conversions,
                metrics.impressions,
                metrics.clicks,
                metrics.ctr
            FROM campaign
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
              AND metrics.cost_micros > 0
            ORDER BY metrics.cost_micros DESC
        """
        rows = _query(customer_id, gaql)

        campaigns  = []
        total_cost = 0.0
        total_conv = 0.0
        for r in rows:
            c    = r.get("campaign", {})
            m    = r.get("metrics", {})
            cost = float(m.get("costMicros", 0)) / 1_000_000
            conv = float(m.get("conversionsValue", 0))
            total_cost += cost
            total_conv += conv
            campaigns.append({
                "campaign":    c.get("name", ""),
                "status":      c.get("status", ""),
                "cost":        round(cost, 2),
                "conv_value":  round(conv, 2),
                "roas":        round(conv / cost, 2) if cost > 0 else 0,
                "conversions": round(float(m.get("conversions", 0)), 1),
                "impressions": int(m.get("impressions", 0)),
                "clicks":      int(m.get("clicks", 0)),
                "ctr_pct":     round(float(m.get("ctr", 0)) * 100, 2),
            })

        result = {
            "customer_id": customer_id,
            "period":      f"{date_from} → {date_to}",
            "total_cost":  round(total_cost, 2),
            "total_conv":  round(total_conv, 2),
            "total_roas":  round(total_conv / total_cost, 2) if total_cost > 0 else 0,
            "campaigns":   campaigns,
        }
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    if name == "get_account_summary":
        gaql = f"""
            SELECT
                metrics.cost_micros,
                metrics.conversions_value,
                metrics.conversions,
                metrics.impressions,
                metrics.clicks
            FROM customer
            WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
        """
        rows       = _query(customer_id, gaql)
        total_cost = sum(float(r.get("metrics", {}).get("costMicros", 0)) / 1_000_000 for r in rows)
        total_conv = sum(float(r.get("metrics", {}).get("conversionsValue", 0)) for r in rows)

        result = {
            "customer_id": customer_id,
            "period":      f"{date_from} → {date_to}",
            "total_cost":  round(total_cost, 2),
            "total_conv":  round(total_conv, 2),
            "roas":        round(total_conv / total_cost, 2) if total_cost > 0 else 0,
        }
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    raise ValueError(f"Tool desconhecida: {name}")


# ─── Starlette app ────────────────────────────────────────────────────────────

sse = SseServerTransport("/messages")


async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def handle_messages(request):
    await sse.handle_post_message(request.scope, request.receive, request._send)


app = Starlette(
    routes=[
        Route("/sse",      endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/health",   endpoint=lambda r: Response("ok")),
    ]
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
