# syntax=docker/dockerfile:1

# Pre-build Tailwind CSS for production (avoids CDN play script in container).
FROM node:20-alpine AS tailwind
WORKDIR /build
COPY static/input.css static/input.css
COPY templates templates
RUN mkdir -p static/dist \
    && npx --yes tailwindcss@3.4.10 \
         -i static/input.css \
         -o static/dist/tailwind.css \
         --minify \
         --content "templates/**/*.html"

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=tailwind /build/static/dist/tailwind.css static/dist/tailwind.css

ENV USE_BUILT_TAILWIND=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
