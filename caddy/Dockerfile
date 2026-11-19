# Caddy with the Porkbun DNS plugin: the certificate comes from a DNS-01 challenge,
# so Let's Encrypt never has to reach inbound port 80 or 443 (the ISP blocks them).
FROM caddy:2.10-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/porkbun

FROM caddy:2.10
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
