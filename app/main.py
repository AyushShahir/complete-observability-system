
import asyncio
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode


# --------------------------------------------------
# 1. APPLICATION SETUP
# --------------------------------------------------

app = FastAPI(
    title="SmartShop API",
    description="Sample e-commerce application for observability",
    version="1.0.0"
)


# --------------------------------------------------
# 2. LOGGING CONFIGURATION
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("smartshop")


# --------------------------------------------------
# 3. OPENTELEMETRY CONFIGURATION
# --------------------------------------------------

# Identify this application in Jaeger
resource = Resource.create({
    "service.name": "smartshop-api"
})

# Create a tracer provider
provider = TracerProvider(resource=resource)

# Send traces to Jaeger using OTLP HTTP
otlp_exporter = OTLPSpanExporter(
    endpoint="http://jaeger:4318/v1/traces"
)

# Batch processor sends collected spans together
provider.add_span_processor(
    BatchSpanProcessor(otlp_exporter)
)

# Register the provider
trace.set_tracer_provider(provider)

# Create our application tracer
tracer = trace.get_tracer("smartshop")

# Automatically trace incoming FastAPI requests
FastAPIInstrumentor.instrument_app(app)


# --------------------------------------------------
# 4. PROMETHEUS METRICS
# --------------------------------------------------

REQUEST_COUNT = Counter(
    "http_requests",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"]
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Time taken to process HTTP requests",
    ["method", "endpoint"]
)

ACTIVE_REQUESTS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed"
)


# --------------------------------------------------
# 5. REQUEST MONITORING MIDDLEWARE
# --------------------------------------------------

@app.middleware("http")
async def monitor_requests(request: Request, call_next):

    # Do not count Prometheus scraping requests
    if request.url.path == "/metrics":
        return await call_next(request)

    start_time = time.perf_counter()
    status_code = 500

    ACTIVE_REQUESTS.inc()

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response

    except Exception:
        logger.exception("Unhandled application exception")
        raise

    finally:
        duration = time.perf_counter() - start_time

        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status_code=str(status_code)
        ).inc()

        REQUEST_LATENCY.labels(
            method=request.method,
            endpoint=request.url.path
        ).observe(duration)

        ACTIVE_REQUESTS.dec()

        logger.info(
            "%s %s | Status: %s | Duration: %.4f seconds",
            request.method,
            request.url.path,
            status_code,
            duration
        )


# --------------------------------------------------
# 6. SAMPLE PRODUCT DATA
# --------------------------------------------------

products = [
    {"id": 1, "name": "Laptop", "price": 55000},
    {"id": 2, "name": "Headphones", "price": 2000},
    {"id": 3, "name": "Keyboard", "price": 1500},
    {"id": 4, "name": "Mouse", "price": 800}
]


# --------------------------------------------------
# 7. ORDER REQUEST MODEL
# --------------------------------------------------

class OrderRequest(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


# --------------------------------------------------
# 8. API ENDPOINTS
# --------------------------------------------------

@app.get("/")
async def home():

    logger.info("SmartShop home page accessed")

    return {
        "message": "Welcome to SmartShop API",
        "status": "running"
    }


@app.get("/health")
async def health():

    logger.info("Health check requested")

    return {
        "status": "healthy"
    }


@app.get("/products")
async def get_products():

    # Custom span for product retrieval
    with tracer.start_as_current_span("products.fetch") as span:

        span.set_attribute("operation.type", "product_retrieval")

        logger.info("Product list requested")

        span.add_event("Product list retrieved")

        return {
            "products": products
        }


@app.post("/orders")
async def create_order(order: OrderRequest):

    # Parent span for the complete order operation
    with tracer.start_as_current_span("orders.process") as order_span:

        order_span.set_attribute(
            "order.product_id",
            order.product_id
        )

        order_span.set_attribute(
            "order.quantity",
            order.quantity
        )

        logger.info(
            "New order requested for product %s",
            order.product_id
        )

        # Child span: search for the requested product
        with tracer.start_as_current_span("orders.find_product") as search_span:

            product = next(
                (p for p in products if p["id"] == order.product_id),
                None
            )

            if product is None:

                search_span.set_status(
                    Status(StatusCode.ERROR, "Product not found")
                )

                logger.warning(
                    "Order failed: product %s not found",
                    order.product_id
                )

                order_span.set_status(
                    Status(StatusCode.ERROR, "Product not found")
                )

                return JSONResponse(
                    status_code=404,
                    content={"error": "Product not found"}
                )

            search_span.set_attribute(
                "product.name",
                product["name"]
            )

        # Child span: calculate the total price
        with tracer.start_as_current_span("orders.calculate_total") as price_span:

            total_price = product["price"] * order.quantity

            price_span.set_attribute(
                "order.total_price",
                total_price
            )

        order_id = str(uuid.uuid4())

        logger.info(
            "Order %s created successfully",
            order_id
        )

        order_span.add_event("Order created successfully")

        return {
            "order_id": order_id,
            "product": product["name"],
            "quantity": order.quantity,
            "total_price": total_price,
            "status": "confirmed"
        }


@app.get("/slow")
async def slow_request():

    # This span will show the five-second delay
    with tracer.start_as_current_span("request.slow_simulation") as span:

        span.set_attribute("delay.seconds", 5)

        logger.warning("Slow request simulation started")

        await asyncio.sleep(5)

        logger.info("Slow request simulation completed")

        return {
            "message": "Slow request completed",
            "delay_seconds": 5
        }


@app.get("/error")
async def error_request():

    with tracer.start_as_current_span("request.simulated_error") as span:

        span.set_status(
            Status(StatusCode.ERROR, "Simulated application failure")
        )

        logger.error("Simulated application failure")

        return JSONResponse(
            status_code=500,
            content={"error": "Simulated internal server error"}
        )


# --------------------------------------------------
# 9. PROMETHEUS METRICS ENDPOINT
# --------------------------------------------------

app.mount("/metrics", make_asgi_app())
