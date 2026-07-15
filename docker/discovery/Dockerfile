FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY eo-services.json /usr/share/nginx/html/.well-known/eo-services.json

EXPOSE 80
