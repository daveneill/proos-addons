FROM node:20-alpine

RUN apk add --no-cache bash curl

WORKDIR /app
COPY package.json ./
RUN npm install --production

COPY server.js ./
COPY run.sh ./
RUN chmod +x run.sh

CMD ["/app/run.sh"]
