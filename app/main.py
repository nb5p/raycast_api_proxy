import json
import logging
import os
from itertools import chain
from pathlib import Path


import httpx
import openai
from google import generativeai as genai
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from app.utils import ProxyRequest, pass_through_request

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI()

logger = logging.getLogger("proxy")

http_client = httpx.AsyncClient()

USER_SESSION = {}  # bearer token -> user email
ALLOWED_USERS = (
    os.environ.get("ALLOWED_USERS").split(",")
    if os.environ.get("ALLOWED_USERS", "")
    else None
)

MAX_TOKENS = os.environ.get("MAX_TOKENS", 1024)


def add_user(request: Request, user_email: str):
    bearer_token = request.headers.get("Authorization", "").split(" ")[1]
    if bearer_token not in USER_SESSION:
        logger.info(f"Adding user {user_email} to session")
        USER_SESSION[bearer_token] = user_email


def check_auth(request: Request):
    if not ALLOWED_USERS:
        return True
    bearer_token = request.headers.get("Authorization", "").split(" ")[1]
    if bearer_token not in USER_SESSION:
        logger.warn(f"User not in session: {bearer_token}")
        return False
    user_email = USER_SESSION[bearer_token]
    if user_email not in ALLOWED_USERS:
        logger.debug(f"Allowed users: {ALLOWED_USERS}")
        logger.warn(f"User not allowed: {user_email}")
        return False
    return True


@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()


FORCE_MODEL = os.environ.get("FORCE_MODEL", None)

SERVICE_PROVIDERS = {
    "openai": [
        {
            "id": "openai-gpt-3.5-turbo",
            "model": "gpt-3.5-turbo",
            "name": "GPT-3.5 Turbo",
            "provider": "openai",
            "provider_name": "OpenAI",
            "requires_better_ai": True,
            "features": [
                "chat",
                "quick_ai",
                "commands",
                "api",
            ],
        },
        {
            "id": "openai-gpt-4-1106-preview",
            "model": "gpt-4-1106-preview",
            "name": "GPT-4 Turbo",
            "provider": "openai",
            "provider_name": "OpenAI",
            "requires_better_ai": True,
            "features": [
                "chat",
                "quick_ai",
                "commands",
                "api",
            ],
        },
    ],
    "google": [
        {
            "id": "gemini-pro",
            "model": "gemini-pro",
            "name": "Gemini Pro",
            "provider": "google",
            "provider_name": "Google",
            "requires_better_ai": True,
            "features": [],
        },
    ],
}

MODEL_PROVIDER_MAP = {
    p["model"]: p["provider"] for p in chain.from_iterable(SERVICE_PROVIDERS.values())
}

openai_api_key = os.environ.get("OPENAI_API_KEY")
google_api_key = os.environ.get("GOOGLE_API_KEY")
if google_api_key:
    genai.configure(api_key=google_api_key)
    logger.info("Using Google API")

    RAYCAST_DEFAULT_MODELS = {
        "chat": "gemini-pro",
        "quick_ai": "gemini-pro",
        "commands": "gemini-pro",
        "api": "gemini-pros",
    }
elif openai_api_key:
    openai.api_key = openai_api_key
    is_azure = openai.api_type in ("azure", "azure_ad", "azuread")
    if is_azure:
        logger.info("Using Azure API")
        openai_client = openai.AzureOpenAI(
            azure_endpoint=os.environ.get("OPENAI_AZURE_ENDPOINT"),
            azure_ad_token_provider=os.environ.get("AZURE_DEPLOYMENT_ID", None),
        )
    else:
        logger.info("Using OpenAI API")
        openai_client = openai.OpenAI()

    RAYCAST_DEFAULT_MODELS = {
        "chat": "openai-gpt-3.5-turbo",
        "quick_ai": "openai-gpt-3.5-turbo",
        "commands": "openai-gpt-3.5-turbo",
        "api": "openai-gpt-3.5-turbo",
    }


def get_model(raycast_data: dict):
    return FORCE_MODEL or raycast_data["model"]


async def chat_completions_openai(raycast_data: dict):
    openai_messages = []
    temperature = os.environ.get("TEMPERATURE", 0.5)
    for msg in raycast_data["messages"]:
        if "system_instructions" in msg["content"]:
            openai_messages.append(
                {
                    "role": "system",
                    "content": msg["content"]["system_instructions"],
                }
            )
        if "command_instructions" in msg["content"]:
            openai_messages.append(
                {
                    "role": "system",
                    "content": msg["content"]["command_instructions"],
                }
            )
        if "additional_system_instructions" in raycast_data:
            openai_messages.append(
                {
                    "role": "system",
                    "content": raycast_data["additional_system_instructions"],
                }
            )
        if "text" in msg["content"]:
            openai_messages.append({"role": "user", "content": msg["content"]["text"]})
        if "temperature" in msg["content"]:
            temperature = msg["content"]["temperature"]

    def openai_stream():
        stream = openai_client.chat.completions.create(
            model=get_model(raycast_data),
            messages=openai_messages,
            max_tokens=MAX_TOKENS,
            n=1,
            stop=None,
            temperature=temperature,
            stream=True,
        )
        for response in stream:
            chunk = response.choices[0]
            if chunk.finish_reason is not None:
                logger.debug(f"OpenAI response finish: {chunk.finish_reason}")
                yield f'data: {json.dumps({"text": "", "finish_reason": chunk.finish_reason})}\n\n'
            if chunk.delta and chunk.delta.content:
                logger.debug(f"OpenAI response chunk: {chunk.delta.content}")
                yield f'data: {json.dumps({"text": chunk.delta.content})}\n\n'

    return StreamingResponse(openai_stream(), media_type="text/event-stream")


async def chat_completions_gemini(raycast_data: dict):
    model = genai.GenerativeModel(get_model(raycast_data))

    google_message = ""
    temperature = os.environ.get("TEMPERATURE", 0.5)
    for msg in raycast_data["messages"]:
        if "system_instructions" in msg["content"]:
            google_message += msg["content"]["system_instructions"] + "\n"
        if "command_instructions" in msg["content"]:
            google_message += msg["content"]["command_instructions"] + "\n"
        if "additional_system_instructions" in raycast_data:
            google_message += raycast_data["additional_system_instructions"] + "\n"
        if "text" in msg["content"]:
            google_message += msg["content"]["text"] + "\n"
        if "temperature" in msg["content"]:
            temperature = msg["content"]["temperature"]

    logger.debug(f"text: {google_message}")
    result = model.generate_content(
        google_message,
        stream=True,
        generation_config=genai.types.GenerationConfig(
            candidate_count=1,
            max_output_tokens=MAX_TOKENS,
            temperature=temperature,
        ),
    )

    def gemini_stream():
        try:
            for chunk in result:
                logger.debug(f"Gemini response chunk: {chunk.text}")
                yield f'data: {json.dumps({"text": chunk.text})}\n\n'
        except genai.types.BlockedPromptException as e:
            logger.debug(f"Gemini response finish: {e}")
            yield f'data: {json.dumps({"text": "", "finish_reason": e})}\n\n'

    return StreamingResponse(gemini_stream(), media_type="text/event-stream")


@app.post("/api/v1/ai/chat_completions")
async def chat_completions(request: Request):
    raycast_data = await request.json()
    if not check_auth(request):
        return Response(status_code=401)
    logger.info(f"Received chat completion request: {raycast_data}")

    model_id = get_model(raycast_data)
    logger.debug(f"Use model id: {model_id}")

    if MODEL_PROVIDER_MAP[model_id] == "openai" and openai_api_key:
        return await chat_completions_openai(raycast_data)
    if MODEL_PROVIDER_MAP[model_id] == "google" and google_api_key:
        return await chat_completions_gemini(raycast_data)


@app.api_route("/api/v1/me", methods=["GET"])
async def proxy(request: Request):
    logger.info("Received request to /api/v1/me")
    headers = {key: value for key, value in request.headers.items()}
    req = ProxyRequest(
        str(request.url),
        request.method,
        headers,
        await request.body(),
        query_params=request.query_params,
    )
    response = await pass_through_request(http_client, req)
    content = response.content
    if response.status_code == 200:
        data = json.loads(content)
        data["eligible_for_pro_features"] = True
        data["has_active_subscription"] = True
        data["eligible_for_ai"] = True
        data["eligible_for_gpt4"] = True
        data["eligible_for_ai_citations"] = True
        data["eligible_for_developer_hub"] = True
        data["eligible_for_application_settings"] = True
        data["publishing_bot"] = True
        data["has_pro_features"] = True
        data["has_better_ai"] = True
        data["can_upgrade_to_pro"] = False
        data["admin"] = True
        add_user(request, data["email"])
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(
        status_code=response.status_code,
        content=content,
        headers=response.headers,
    )


@app.api_route("/api/v1/ai/models", methods=["GET"])
async def proxy_models(request: Request):
    logger.info("Received request to /api/v1/ai/models")
    headers = {key: value for key, value in request.headers.items()}
    req = ProxyRequest(
        str(request.url),
        request.method,
        headers,
        await request.body(),
        query_params=request.query_params,
    )
    response = await pass_through_request(http_client, req)
    content = response.content
    if response.status_code == 200:
        data = json.loads(content)
        data["default_models"] = RAYCAST_DEFAULT_MODELS
        data["models"] = list(chain.from_iterable(SERVICE_PROVIDERS.values()))
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(
        status_code=response.status_code,
        content=content,
        headers=response.headers,
    )


# pass through all other requests
@app.api_route("/{path:path}")
async def proxy_options(request: Request, path: str):
    logger.info(f"Received request: {request.method} {path}")
    headers = {key: value for key, value in request.headers.items()}
    url = str(request.url)
    # add https when running via https gateway
    if "https://" not in url:
        url = url.replace("http://", "https://")
    req = ProxyRequest(
        str(request.url),
        request.method,
        headers,
        await request.body(),
        query_params=request.query_params,
    )
    response = await pass_through_request(http_client, req)
    return Response(
        status_code=response.status_code,
        content=response.content,
        headers=response.headers,
    )


if __name__ == "__main__":
    import uvicorn

    current_dir = Path(__file__).parent.parent

    if os.environ.get("CERT_FILE") and os.environ.get("KEY_FILE"):
        ssl_cert_path = Path(os.environ.get("CERT_FILE"))
        ssl_key_path = Path(os.environ.get("KEY_FILE"))
    elif (current_dir / "cert").exists():
        ssl_cert_path = current_dir / "cert" / "backend.raycast.com.cert.pem"
        ssl_key_path = current_dir / "cert" / "backend.raycast.com.key.pem"
    else:
        ssl_cert_path = None
        ssl_key_path = None

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=443,
        ssl_certfile=ssl_cert_path,
        ssl_keyfile=ssl_key_path,
    )
