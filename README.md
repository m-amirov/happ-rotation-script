# Happ Rotation Script

Генератор JSON-конфигурации для **Happ / Xray**, который объединяет несколько прокси в один пул и распределяет **новые соединения** между ними.

По умолчанию используется `roundRobin`:

```text
новое соединение #1 -> proxy-001
новое соединение #2 -> proxy-002
новое соединение #3 -> proxy-003
новое соединение #4 -> proxy-001
```

Это **не таймер смены IP**. Уже открытый HTTPS/WebSocket/QUIC-сеанс не переносится на другой прокси посреди соединения. Ротация происходит при создании новых outbound-соединений Xray.

## Что делает конфигурация

- российские домены `geosite:category-ru` -> `DIRECT`;
- российские IP `geoip:ru` -> `DIRECT`;
- LAN, loopback и private IP -> `DIRECT` (важно для локальной сети и VDI);
- остальной TCP/UDP-трафик -> пул прокси;
- недоступные прокси отслеживаются через Xray `observatory`;
- если все прокси недоступны, по умолчанию используется `BLOCK`, а не `DIRECT`, чтобы зарубежный трафик не ушёл наружу незаметно;
- локальные входы: SOCKS5 `127.0.0.1:10808` и HTTP `127.0.0.1:10809`.

## Поддерживаемые входные ссылки

Скрипт понимает:

- `vless://...`
- `vmess://...` (обычный base64 JSON)
- `trojan://...`
- `socks://...` и `socks5://...`
- `ss://...` (Shadowsocks SIP002)
- raw Xray outbound JSON одной строкой — для нестандартных протоколов/транспортов.

Для необычной конфигурации, которую парсер не понимает, добавьте в `proxies.txt` готовый Xray outbound одной строкой, например:

```json
{"protocol":"socks","settings":{"address":"127.0.0.1","port":2080}}
```

## 1. Требования

- Windows 10/11;
- Happ Desktop;
- Python 3.10+.

Проверка Python:

```powershell
py --version
```

Внешние Python-библиотеки не нужны.

## 2. Скачать репозиторий

```powershell
git clone https://github.com/m-amirov/happ-rotation-script.git
cd happ-rotation-script
```

Либо скачайте ZIP с GitHub и распакуйте его.

## 3. Создать список прокси

Скопируйте пример:

```powershell
Copy-Item .\proxies.example.txt .\proxies.txt
notepad .\proxies.txt
```

В `proxies.txt` оставьте по одному реальному прокси на строку:

```text
vless://UUID@server-1.example:443?encryption=none&security=reality&type=tcp&sni=example.com&fp=chrome&pbk=PUBLIC_KEY&sid=SHORT_ID&flow=xtls-rprx-vision#server-1
vless://UUID@server-2.example:443?encryption=none&security=reality&type=tcp&sni=example.com&fp=chrome&pbk=PUBLIC_KEY&sid=SHORT_ID&flow=xtls-rprx-vision#server-2
socks://login:password@proxy.example:1080#server-3
```

`proxies.txt` добавлен в `.gitignore`, поэтому реальные логины, пароли, UUID и ключи не должны попасть в Git.

## 4. Сгенерировать конфигурацию

```powershell
py .\happ_rotation.py
```

Будет создан файл:

```text
happ-rotation.json
```

Явный вариант той же команды:

```powershell
py .\happ_rotation.py --input .\proxies.txt --output .\happ-rotation.json --strategy roundRobin
```

## 5. Импортировать в Happ

Happ передаёт JSON-конфигурации в Xray напрямую, поэтому правила ротации и маршрутизации находятся внутри `happ-rotation.json`.

1. Откройте `happ-rotation.json` и скопируйте весь JSON.
2. В Happ нажмите `+` и импортируйте конфигурацию из буфера обмена / JSON (название пункта может немного отличаться между версиями Desktop).
3. Выберите созданный профиль **Happ Rotation**.
4. Переподключите профиль.
5. Для Desktop используйте Xray-совместимый режим. Если используете системный proxy, локальные порты уже заданы: SOCKS5 `10808`, HTTP `10809`.

> Для JSON-профиля стандартные routing-настройки Happ не подмешиваются в конфигурацию: JSON передаётся core 1:1. Поэтому менять маршрутизацию нужно в этом генераторе/JSON, а после изменений переподключать профиль.

## Стратегии ротации

### Round robin — рекомендуется для обычной ротации

```powershell
py .\happ_rotation.py --strategy roundRobin
```

Новые соединения последовательно распределяются по прокси.

### Random

```powershell
py .\happ_rotation.py --strategy random
```

Каждое новое соединение получает случайный доступный outbound.

### Lowest ping

```powershell
py .\happ_rotation.py --strategy leastPing
```

Xray выбирает доступный outbound с минимальной задержкой по данным `observatory`. Это скорее балансировка по качеству, чем равномерная ротация.

### Least load

```powershell
py .\happ_rotation.py --strategy leastLoad
```

Xray выбирает наиболее стабильные/подходящие узлы на основании наблюдений.

## Если российские сайты тоже должны идти через прокси

```powershell
py .\happ_rotation.py --no-direct-ru
```

Private/LAN сети всё равно остаются `DIRECT`.

## Что делать, если все прокси недоступны

По умолчанию генератор работает fail-closed:

```powershell
py .\happ_rotation.py --fallback block
```

Если вы сознательно хотите разрешить обычный интернет при полном отказе пула:

```powershell
py .\happ_rotation.py --fallback direct
```

Второй режим может раскрыть реальный внешний IP для зарубежных ресурсов.

## Изменение локальных портов

```powershell
py .\happ_rotation.py --socks-port 11808 --http-port 11809
```

## Частота проверки прокси

По умолчанию Xray проверяет outbound-узлы каждые `30s`:

```powershell
py .\happ_rotation.py --probe-interval 30s
```

Например:

```powershell
py .\happ_rotation.py --probe-interval 1m
```

Не ставьте слишком маленький интервал без необходимости: постоянные тестовые запросы создают дополнительный трафик.

## Проверка результата

Синтаксическая проверка JSON:

```powershell
py -m json.tool .\happ-rotation.json > $null
```

Тесты проекта:

```powershell
py -m unittest discover -s tests -v
```

## Важные ограничения

1. Ротация работает **на новых соединениях**, а не строго «раз в N минут».
2. Сайты часто держат HTTP/2, QUIC или WebSocket открытыми долго, поэтому один сайт некоторое время может продолжать видеть тот же IP.
3. `geosite:category-ru` и `geoip:ru` требуют соответствующих geo-файлов в Xray/Happ. Happ поставляется с geo-файлами и управляет ими на уровне приложения.
4. Некоторые нестандартные URL-параметры провайдера невозможно безошибочно преобразовать автоматически. Для таких узлов используйте raw Xray outbound JSON.
5. SOCKS5 сам по себе не шифрует соединение до SOCKS-сервера. Не используйте незашифрованный публичный SOCKS5 там, где это создаёт риск.

## Почему используется JSON

Happ поддерживает JSON-конфигурации и передаёт их в Xray без преобразования. Xray, в свою очередь, имеет штатные `routing.balancers` со стратегиями `roundRobin`, `random`, `leastPing` и `leastLoad`, а `observatory` используется для проверки состояния outbound-узлов.

Документация:

- Happ JSON/config examples: https://github.com/HappDev/happ_su/blob/main/dev-docs/examples-of-links-and-parameters.md
- Happ routing: https://github.com/HappDev/happ_su/blob/main/dev-docs/routing.md
- Xray routing/balancers: https://xtls.github.io/en/config/routing
- Xray observatory: https://xtls.github.io/en/config/observatory.html
