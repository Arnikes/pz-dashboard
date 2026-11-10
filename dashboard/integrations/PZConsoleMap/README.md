# PZ Console Player Map integration (Build 42, v1.0.0)

Mod ID: `PZConsoleMap`. English instructions are followed by Russian below.

This optional local mod supplies real player positions to PZ Console. Its only
Lua script runs on the dedicated server, reads the online player API and writes
two small JSON snapshots in the game's cachedir `Lua` directory every five
seconds. It has no client/shared Lua, RCON commands, network listener, or gameplay
changes. The dashboard continues to use RCON `players` for online membership.
The map interface is in PZ Console; this mod does not add an in-game map.

## Install once

Choose the actual mod directory from the archive you downloaded:

- **Workshop release ZIP:** the outer `PZConsoleMap` is an upload project. The
  installable mod is its **`Contents/mods/PZConsoleMap`** directory.
- **PZ Console integration ZIP:** its root **`PZConsoleMap`** is already the
  installable mod directory.

1. Keep that actual mod directory intact, including `common/` and
   `42/`. Copy it into the game server's **cachedir** `mods` directory, beside its
   `Server`, `Saves`, and `Lua` directories. The result must be
   `<cachedir>/mods/PZConsoleMap/42/mod.info`.
2. Stop the server through its usual maintenance flow. Append `\PZConsoleMap` to
   the existing `Mods` setting of the **active** Build 42 server profile, preserving
   other IDs and their order. For example, `Mods=\ExistingMod;\PZConsoleMap`.
   This is a local mod: it has no Workshop ID to put in `WorkshopItems`.
   If your game image owns `Mods` through an environment variable, update that
   variable instead of the generated INI file.
3. Start the server. The dashboard and game must share the same cachedir volume
   (`/data` in the dashboard). Open **Players**; positions should arrive after
   the first game tick and the next dashboard refresh.

The supplied new-server Compose example uses `/project-zomboid-config` as the
game cachedir. From the directory where you extracted the **PZ Console
integration ZIP**, copy its actual mod directory:

```bash
docker exec pzserver mkdir -p /project-zomboid-config/mods
docker cp PZConsoleMap pzserver:/project-zomboid-config/mods/PZConsoleMap
```

For the **Workshop release ZIP**, run the same `mkdir` command and copy the inner
mod instead:

```bash
docker cp PZConsoleMap/Contents/mods/PZConsoleMap pzserver:/project-zomboid-config/mods/PZConsoleMap
```

Use your actual container and cachedir if they differ. Copy only this integration;
do not replace an existing `mods` directory. Multiplayer mod availability checks
belong to the game: if joining clients require the local mod, give them this same
directory for their own `Zomboid/mods` folder. The script exits on clients.

## Base map and freshness

For real terrain outlines, mount the game installation read-only at
`/server-files` in the dashboard (or set `SERVER_FILES_DIR`). The panel reads
`media/maps/<active Map>/worldmap.xml` and available active mod map files. It does
not download map tiles or redistribute game map assets.

Vector XML declaring `cellSize="256"` or `cellSize="300"` carries its coordinate
scale. If your XML does not declare it, set the **dashboard** environment variable
`PZ_MAP_CELL_SIZE=256` or `PZ_MAP_CELL_SIZE=300` to the scale used by that map's
exporter, then recreate the dashboard. Do not infer this value from compiled map
files. Without a confirmed scale the panel reports the missing setup rather
than placing terrain at guessed coordinates.

Snapshots expire after 15 seconds. Stale positions retain their date and lose
their current status. Missing, truncated, invalid, or mismatched data is shown
explicitly. Only exact online usernames can appear as current markers. Server
profile names are limited to 96 UTF-8 bytes for portable snapshot filenames.
RCON-only remote connections need shared snapshot/map files for this feature.

## Troubleshooting and removal

- **Integration missing:** verify the mod ID, active profile, directory layout,
  cachedir mount, and the game's permission to write JSON files under `Lua`.
- **Profile unavailable/mismatch:** set the dashboard's `PZ_CONFIG_FILE` to the
  active INI filename if startup detection cannot identify it. Changing the
  profile selected for configuration editing does not change the running world.
- **Stale:** check that the game is ticking and the snapshot files are being
  updated. A stopped/paused game or unavailable shared volume cannot supply
  current locations; the panel never replays an old capture as current.
- **Map unavailable/partial:** check `SERVER_FILES_DIR`, active map/mod
  installation, XML scale, and the map's supported XML schema/size limits.
- **Unverified mod map layering:** active XML source extents overlap. Real
  geometry remains visible, but replacement-map composition has not been verified
  against the running game; the preview can contain overlapping outlines.
- **Some positions missing:** a character may not have a valid position yet, an
  online list may have changed between captures, or the 64-player/128 KiB snapshot
  bound may have been reached. Other roster actions continue to work.

To remove the integration, stop the game, remove only `PZConsoleMap` from the
active `Mods` list, and remove its local mod directory. Its optional snapshot
files have names `pz-console-map-<hex-encoded-profile>-a.json` and `-b.json` in
cachedir `Lua`; they contain player names/coordinates and may be deleted after
the mod is disabled. No world save is modified by this integration.

The cover is original AI-generated illustrative artwork, not a game screenshot
or an actual world map. Live-game acceptance and Steam publication are separate
steps from the local Lua and packaging checks.

## Русский

Этот необязательный мод выделенного сервера передаёт позиции игроков на карту во
вкладке **Players** в **PZ Console**. Карту отображает панель. Мод не добавляет
карту в игру и не запускает отдельный веб-сервис.

### Однократная установка

Выберите настоящую папку мода в скачанном архиве:

- **Workshop release ZIP:** внешняя папка `PZConsoleMap` — проект загрузки в
  Workshop. Для локальной установки нужна её **`Contents/mods/PZConsoleMap`**.
- **ZIP интеграции из PZ Console:** корневая **`PZConsoleMap`** уже является
  настоящей папкой мода.

1. Остановите игровой сервер обычным способом обслуживания.
2. Сохраните структуру настоящей папки мода, включая `common/` и `42/`. Для
   локальной установки скопируйте её в `mods` внутри **cachedir** сервера, рядом
   с `Server`, `Saves` и `Lua`. Итоговый путь:
   `<cachedir>/mods/PZConsoleMap/42/mod.info`.
3. Добавьте `\PZConsoleMap` к существующему списку `Mods` **активного** профиля
   Build 42, сохранив остальные ID и порядок. Если контейнер формирует `Mods` из
   переменной окружения, измените её вместо генерируемого INI. Для локальной
   установки запись в `WorkshopItems` не нужна.
4. Запустите сервер. Игра и PZ Console должны использовать общий том cachedir
   (`/data` в панели). Откройте **Players** после первого игрового тика и
   следующего обновления данных панели.

После настоящей публикации в Workshop добавляйте в `WorkshopItems` только ID,
возвращённый Steam. Mod ID остаётся `PZConsoleMap`. Игра может потребовать
установку того же мода у подключающихся клиентов, хотя его единственный Lua-скрипт
выполняется на сервере и завершает работу на клиенте.

### Файлы карты и актуальность

Раз в пять секунд мод получает точные имена онлайн-игроков и координаты X/Y/Z.
Два чередующихся JSON-файла в `Lua` внутри cachedir позволяют пережить чтение во
время записи или ошибку записи. Один снимок ограничен 64 игроками и 128 КиБ.
Имя профиля сервера — не более 96 байт UTF-8.

PZ Console проверяет онлайн по текущему списку RCON `players` и помечает снимки
старше 15 секунд как устаревшие. Отсутствующие, повреждённые, чужие профилю и
доперезапусковые снимки не выдаются за актуальные позиции. Локальные файлы содержат
имена и координаты для панели администратора. В них нет учётных данных Steam.
Мод не открывает сетевой интерфейс, не добавляет RCON-команды, клиентские/общие
Lua-скрипты, изменения игрового процесса или запись в сохранения мира.

Для отображения местности подключите каталог игры в панель только для чтения
как `/server-files` либо задайте `SERVER_FILES_DIR`. Панель читает установленный
XML карты и не загружает чужие тайлы. Значение `cellSize="256"` или
`cellSize="300"` в XML задаёт масштаб. Если его нет, укажите в окружении панели
`PZ_MAP_CELL_SIZE` со значением `256` или `300`, подтверждённым экспортёром
векторной карты, и пересоздайте панель. Размер скомпилированной карты не определяет
этот параметр.

Одного удалённого RCON без общих файлов снимков недостаточно. Для перекрывающихся
источников мод-карт панель предупреждает о непроверенном наложении: предпросмотр
не гарантирует совпадения замены карт с работающей игрой.

### Диагностика и удаление

- **Мод не обнаружен:** проверьте активный Mod ID, структуру папок, общий cachedir
  и разрешение игры на запись JSON в `Lua`.
- **Профиль не определён:** если обнаружение запуска не помогло, задайте в панели
  `PZ_CONFIG_FILE` с именем активного INI. Выбор неактивного профиля в редакторе не
  переключает работающий мир.
- **Координаты устарели:** проверьте игровые тики и обновление общих файлов.
  Переподключение панели само по себе не делает старый снимок актуальным.
- **Карта недоступна или неполна:** проверьте активные карты, подключение файлов
  только для чтения, масштаб XML и сообщения панели о формате/размере.
- **Есть не все позиции:** список онлайн мог измениться между снимками, персонаж
  мог ещё не получить координаты либо достигнут предел снимка.

Для удаления остановите игру, уберите только `PZConsoleMap` из `Mods` и удалите
его локальную папку. После отключения можно удалить его необязательные файлы
`pz-console-map-<hex-profile>-a.json` и `-b.json` из `Lua` внутри cachedir.
Остальные моды и сохранения игры сохраните.

Обложка — оригинальная иллюстрация, созданная с помощью ИИ, а не скриншот игры
или настоящая карта мира. Приёмка на работающей игре и публикация в Steam
выполняются отдельно от локальных проверок Lua и упаковки.

## License / Лицензия

MIT. Copyright (c) 2026 Arnike. See the included `LICENSE` file / см. файл `LICENSE`.
