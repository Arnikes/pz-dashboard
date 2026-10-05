# Локальные шрифты

Все WOFF2-файлы хранятся в репозитории и копируются в Docker-образ вместе
с каталогом `static`. Браузер получает их с того же сервера, что и пульт;
загрузок с Google Fonts или других CDN нет.

Единые объявления `@font-face` находятся в `fonts.css`. Все три HTML-страницы
подключают этот файл перед `style.css`; пути WOFF2 заданы относительно `fonts.css`.
Не добавляйте внешние `@import`, `src`, `preconnect` или скрипты для ресурсов UI.

| Семейство | Начертания | Наборы символов | Лицензия |
|---|---|---|---|
| Golos Text | 400, 500, 600 | Latin, Cyrillic | [SIL OFL](golostext-OFL.txt) |
| JetBrains Mono | 400, 500, 700 | Latin, Cyrillic | [SIL OFL](jetbrainsmono-OFL.txt) |
| Russo One | 400 | Latin, Cyrillic | [SIL OFL](russoone-OFL.txt) |

Golos Text и JetBrains Mono используются текущим интерфейсом. Russo One сохранён
в комплекте для прежнего оформления и не загружается, пока не используется.
Имена отдельных файлов сохраняют совместимость с существующими подключениями.

Копии лицензий получены из официального репозитория Google Fonts:
[Golos Text](https://github.com/google/fonts/tree/main/ofl/golostext),
[JetBrains Mono](https://github.com/google/fonts/tree/main/ofl/jetbrainsmono),
[Russo One](https://github.com/google/fonts/tree/main/ofl/russoone).
Это ссылки на происхождение ресурсов; приложение не обращается к ним при работе.

Проверка `tests/browser/test_bundled_assets.py` запрещает внешние запросы,
открывает вход, все разделы пульта и справку, а также проверяет реальную загрузку
каждого локального начертания с латиницей и кириллицей.
