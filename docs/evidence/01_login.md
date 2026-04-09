# Evidence: User Login & WebSocket Connection

## What this shows
- Driver authenticates via `POST /api/auth/login` → user-registry issues JWT
- nginx proxies the request and logs it
- On successful login, frontend opens a WebSocket connection to notification-service
- journey-management fetches the driver's existing bookings (scatter-gather across all 3 regional data-services)

## Docker logs

```
user-registry                | INFO:     172.19.0.29:52952 - "POST /auth/login HTTP/1.0" 200 OK
nginx                        | 172.19.0.1 - - [09/Apr/2026:23:26:12 +0000] "POST /api/auth/login HTTP/1.1" 200 254 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
notification-service         | INFO:     172.19.0.29:60414 - "WebSocket /ws/user" [accepted]
notification-service         | INFO:     connection open
data-service-laos            | INFO:     172.19.0.28:44946 - "GET /bookings/driver/user HTTP/1.1" 200 OK
data-service-cambodia        | INFO:     172.19.0.28:36464 - "GET /bookings/driver/user HTTP/1.1" 200 OK
data-service-andorra         | INFO:     172.19.0.28:36566 - "GET /bookings/driver/user HTTP/1.1" 200 OK
journey-management           | INFO:     172.19.0.29:33226 - "GET /journeys HTTP/1.0" 200 OK
nginx                        | 172.19.0.1 - - [09/Apr/2026:23:26:13 +0000] "GET /api/journeys HTTP/1.1" 200 298121 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Key observations
- `user-registry` returns 200 — credentials valid, JWT issued
- `notification-service` immediately accepts the WebSocket — driver is now subscribed for live booking outcomes
- journey-management queries **all three** regional data-services (`data-service-laos`, `data-service-cambodia`, `data-service-andorra`) in parallel to build the driver's full booking history — this is the scatter-gather pattern for cross-regional booking visibility
