"""Version-scoped B42 metadata derived from the profile, without executing Lua."""

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from configformats import FormatError, LuaTable, SECRET_KEY, ini_entries

API_INVENTORY = json.loads(
    (Path(__file__).parent / "schemas/b42-api.json").read_text(encoding="utf-8")
)["tables"]

INI = {
    "PublicName": ("Название сервера", "Доступ и игроки", "string", None, None),
    "PublicDescription": ("Описание сервера", "Доступ и игроки", "string", None, None),
    "Public": ("Публичный сервер", "Доступ и игроки", "boolean", None, None),
    "Open": ("Свободный вход", "Доступ и игроки", "boolean", None, None),
    "Password": ("Пароль входа", "Доступ и игроки", "string", None, None),
    "MaxPlayers": ("Лимит игроков", "Доступ и игроки", "integer", 1, 100),
    "PVP": ("PvP", "PvP", "boolean", None, None),
    "SafetySystem": ("Система безопасности PvP", "PvP", "boolean", None, None),
    "GlobalChat": ("Глобальный чат", "Чат", "boolean", None, None),
    "SaveWorldEveryMinutes": ("Сохранение мира, минуты", "Сохранение мира", "integer", 0, None),
    "PlayerSafehouse": ("Безопасные дома игроков", "Безопасные дома", "boolean", None, None),
    "SafehouseAllowTrepass": (
        "Разрешить вход в безопасные дома",
        "Безопасные дома",
        "boolean",
        None,
        None,
    ),
    "DefaultPort": ("Игровой порт", "Сеть", "integer", 1, 65535),
    "UDPPort": ("UDP-порт", "Сеть", "integer", 1, 65535),
    "RCONPort": ("RCON-порт", "Сеть", "integer", 1, 65535),
    "RCONPassword": ("Пароль RCON", "Сеть", "string", None, None),
}

# Numeric choices are the game's stored enum values, not zero-based UI indexes.
SANDBOX = {
    "Zombies": {
        "label": "Количество зомби",
        "group": "Зомби",
        "choices": ["Безумное", "Очень много", "Много", "Обычное", "Мало", "Нет"],
    },
    "Distribution": {
        "label": "Распределение зомби",
        "group": "Зомби",
        "choices": ["Городское", "Равномерное"],
    },
    "ZombieLore.Speed": {
        "label": "Скорость зомби",
        "group": "Зомби",
        "choices": ["Бегуны", "Быстрые", "Медленные", "Случайная"],
    },
    "ZombieLore.Strength": {
        "label": "Сила зомби",
        "group": "Зомби",
        "choices": ["Сверхсила", "Обычная", "Слабая", "Случайная"],
    },
    "ZombieConfig.PopulationMultiplier": {
        "label": "Множитель популяции",
        "group": "Зомби",
        "min": 0,
        "max": 4,
    },
    "ZombieConfig.PopulationPeakDay": {
        "label": "День пика популяции",
        "group": "Зомби",
        "min": 1,
        "max": 365,
    },
    "StartYear": {"label": "Год начала", "group": "Начало мира", "newWorld": True},
    "StartMonth": {
        "label": "Месяц начала",
        "group": "Начало мира",
        "min": 1,
        "max": 12,
        "newWorld": True,
    },
    "StartDay": {
        "label": "День начала",
        "group": "Начало мира",
        "min": 1,
        "max": 31,
        "newWorld": True,
    },
    "StartTime": {"label": "Время начала", "group": "Начало мира", "newWorld": True},
    "FoodLootNew": {"label": "Добыча еды", "group": "Добыча", "min": 0},
    "WeaponLootNew": {"label": "Добыча оружия", "group": "Добыча", "min": 0},
    "AmmoLootNew": {"label": "Добыча патронов", "group": "Добыча", "min": 0},
    "MultiHitZombies": {"label": "Мультиудар", "group": "Персонажи"},
    "StarterKit": {"label": "Стартовый набор", "group": "Персонажи", "newWorld": True},
    "EnableVehicles": {"label": "Транспорт", "group": "Транспорт"},
    "VehicleEasyUse": {"label": "Простое использование транспорта", "group": "Транспорт"},
    "AnimalSoundAttractZombies": {"label": "Звуки животных привлекают зомби", "group": "Животные"},
    "PlantGrowingSeasons": {"label": "Сезоны роста растений", "group": "Природа"},
}

# Presentation metadata does not invent numeric limits. Game-generated comments
# in the original profile supply the exact choices and bounds for that revision.
WORLD_LABELS = {
    "DayLength": ("Длительность дня", "Время и климат"),
    "Temperature": ("Температура", "Время и климат"),
    "Rain": ("Частота дождей", "Время и климат"),
    "WaterShut": ("Отключение воды", "Коммунальные услуги"),
    "ElecShut": ("Отключение электричества", "Коммунальные услуги"),
    "WaterShutModifier": ("День отключения воды", "Коммунальные услуги"),
    "ElecShutModifier": ("День отключения электричества", "Коммунальные услуги"),
    "AllowExteriorGenerator": ("Генераторы снаружи зданий", "Коммунальные услуги"),
    "GeneratorFuelConsumption": ("Расход топлива генератора", "Коммунальные услуги"),
    "GeneratorSpawning": ("Частота генераторов", "Коммунальные услуги"),
    "ErosionSpeed": ("Скорость зарастания", "Природа"),
    "ErosionDays": ("Дни до полного зарастания", "Природа"),
    "Farming": ("Рост растений", "Природа"),
    "PlantResilience": ("Устойчивость растений", "Природа"),
    "PlantAbundance": ("Количество растений", "Природа"),
    "NatureAbundance": ("Богатство природы", "Природа"),
    "CompostTime": ("Время приготовления компоста", "Природа"),
    "FishAbundance": ("Количество рыбы", "Природа"),
    "FoodLoot": ("Добыча еды", "Добыча"),
    "CannedFoodLoot": ("Добыча консервов", "Добыча"),
    "CannedFoodLootNew": ("Добыча консервов", "Добыча"),
    "LiteratureLoot": ("Добыча книг", "Добыча"),
    "LiteratureLootNew": ("Добыча книг", "Добыча"),
    "SurvivalGearsLoot": ("Снаряжение для выживания", "Добыча"),
    "SurvivalGearsLootNew": ("Снаряжение для выживания", "Добыча"),
    "MedicalLoot": ("Медицинские припасы", "Добыча"),
    "MedicalLootNew": ("Медицинские припасы", "Добыча"),
    "WeaponLoot": ("Добыча оружия", "Добыча"),
    "RangedWeaponLoot": ("Добыча огнестрельного оружия", "Добыча"),
    "RangedWeaponLootNew": ("Добыча огнестрельного оружия", "Добыча"),
    "AmmoLoot": ("Добыча патронов", "Добыча"),
    "MechanicsLoot": ("Запчасти", "Добыча"),
    "MechanicsLootNew": ("Запчасти", "Добыча"),
    "OtherLoot": ("Прочая добыча", "Добыча"),
    "OtherLootNew": ("Прочая добыча", "Добыча"),
    "LootRespawn": ("Обновление добычи", "Добыча"),
    "SeenHoursPreventLootRespawn": ("Защита посещённых мест от обновления добычи", "Добыча"),
    "MaximumLootedBuildingRooms": ("Предел комнат для разграбленных зданий", "Добыча"),
    "ZombiePopLootEffect": ("Влияние популяции зомби на добычу", "Добыча"),
    "XpMultiplier": ("Множитель опыта", "Персонажи"),
    "XpMultiplierAffectsPassive": ("Множитель опыта для пассивных навыков", "Персонажи"),
    "CharacterFreePoints": ("Дополнительные очки персонажа", "Персонажи"),
    "Nutrition": ("Питание", "Персонажи"),
    "FoodRotSpeed": ("Скорость порчи еды", "Еда и предметы"),
    "FridgeFactor": ("Эффективность холодильников", "Еда и предметы"),
    "FoodRemovalList": ("Удаляемые предметы", "Еда и предметы"),
    "WorldItemRemovalList": ("Предметы для очистки мира", "Еда и предметы"),
    "HoursForWorldItemRemoval": ("Время до удаления предметов", "Еда и предметы"),
    "ItemRemovalListBlacklistToggle": ("Список предметов — исключения", "Еда и предметы"),
    "DaysForRottenFoodRemoval": ("Дни до удаления испорченной еды", "Еда и предметы"),
    "AllowMiniMap": ("Мини-карта", "Карта и интерфейс"),
    "AllowWorldMap": ("Карта мира", "Карта и интерфейс"),
    "MapAllKnown": ("Открытая карта", "Карта и интерфейс"),
    "AnnotatedMapChance": ("Частота карт с пометками", "Карта и интерфейс"),
    "VehicleStoryChance": ("События с транспортом", "Транспорт"),
    "ZoneStoryChance": ("События в мире", "События мира"),
    "RandomizedHouseChance": ("Случайные дома", "События мира"),
    "SurvivorHouseChance": ("Дома выживших", "События мира"),
    "Helicopter": ("Вертолёт", "События мира"),
    "MetaEvent": ("События вне поля зрения", "События мира"),
    "SleepingEvent": ("События во время сна", "События мира"),
    "HouseAlarmFrequency": ("Частота сигнализаций", "События мира"),
    "LockedHouses": ("Запертые дома", "События мира"),
    "CarSpawnRate": ("Количество машин", "Транспорт"),
    "CarGasConsumption": ("Расход топлива машин", "Транспорт"),
    "LockedCar": ("Запертые машины", "Транспорт"),
    "CarGeneralCondition": ("Состояние машин", "Транспорт"),
    "CarDamageOnImpact": ("Повреждения машин при столкновении", "Транспорт"),
    "DamageToPlayerFromHitByACar": ("Урон от наезда", "Транспорт"),
    "TrafficJam": ("Пробки", "Транспорт"),
    "ChanceHasGas": ("Машины с топливом", "Транспорт"),
    "InitialGas": ("Начальный запас топлива", "Транспорт"),
    "FuelStationGas": ("Топливо на заправках", "Транспорт"),
    "RecentlySurvivorVehicles": ("Машины выживших", "Транспорт"),
    "AnimalRanchChance": ("Фермы с животными", "Животные"),
    "AnimalMilkIncModifier": ("Производство молока", "Животные"),
    "AnimalWoolIncModifier": ("Рост шерсти", "Животные"),
    "AnimalPregnancyTime": ("Продолжительность беременности животных", "Животные"),
    "AnimalAgeModifier": ("Скорость взросления животных", "Животные"),
    "AnimalMetaStatsModifier": ("Потребности животных", "Животные"),
    "AnimalMatingSeason": ("Сезонность размножения", "Животные"),
    "AnimalEggHatch": ("Вылупление из яиц", "Животные"),
    "AnimalEggHatchTime": ("Время вылупления из яиц", "Животные"),
    "AnimalTrackChance": ("Следы животных", "Животные"),
    "AnimalPathChance": ("Маршруты животных", "Животные"),
    "AnimalImpactedByHeadlights": ("Реакция животных на фары", "Животные"),
    "AnimalStatsModifier": ("Характеристики животных", "Животные"),
    "ZombieLore.Cognition": ("Интеллект зомби", "Зомби"),
    "ZombieLore.Memory": ("Память зомби", "Зомби"),
    "ZombieLore.Sight": ("Зрение зомби", "Зомби"),
    "ZombieLore.Hearing": ("Слух зомби", "Зомби"),
    "ZombieLore.Toughness": ("Прочность зомби", "Зомби"),
    "ZombieLore.Transmission": ("Передача инфекции", "Зомби"),
    "ZombieLore.Mortality": ("Смертность от инфекции", "Зомби"),
    "ZombieLore.Reanimate": ("Время до превращения", "Зомби"),
    "ZombieLore.ThumpNoChasing": ("Разрушение препятствий без погони", "Зомби"),
    "ZombieLore.ThumpOnConstruction": ("Атаки построек", "Зомби"),
    "ZombieLore.ActiveOnly": ("Активность зомби", "Зомби"),
    "ZombieLore.TriggerHouseAlarm": ("Зомби включают сигнализацию", "Зомби"),
    "ZombieConfig.PopulationStartMultiplier": ("Начальная популяция", "Зомби"),
    "ZombieConfig.PopulationPeakMultiplier": ("Популяция на пике", "Зомби"),
    "ZombieConfig.RespawnHours": ("Интервал возрождения", "Зомби"),
    "ZombieConfig.RespawnUnseenHours": ("Возрождение в непосещённых областях", "Зомби"),
    "ZombieConfig.RespawnMultiplier": ("Доля возрождающихся зомби", "Зомби"),
    "ZombieConfig.RedistributeHours": ("Перераспределение зомби", "Зомби"),
    "ZombieConfig.FollowSoundDistance": ("Дальность реакции на звук", "Зомби"),
    "ZombieConfig.RallyGroupSize": ("Размер групп зомби", "Зомби"),
    "ZombieConfig.RallyTravelDistance": ("Дальность движения групп", "Зомби"),
    "ZombieConfig.RallyGroupSeparation": ("Расстояние между группами", "Зомби"),
    "ZombieConfig.RallyGroupRadius": ("Радиус группы зомби", "Зомби"),
    "MultiplierConfig.Global": ("Общий множитель опыта", "Персонажи"),
    "MultiplierConfig.GlobalToggle": ("Общий множитель для всех навыков", "Персонажи"),
    "Map.AllowMiniMap": ("Мини-карта", "Карта и интерфейс"),
    "Map.AllowWorldMap": ("Карта мира", "Карта и интерфейс"),
    "Map.MapAllKnown": ("Открытая карта", "Карта и интерфейс"),
}

INI_PRESENTATION = {
    "ServerWelcomeMessage": ("Приветствие игрока", "Чат", "multiline"),
    "PublicDescription": ("Описание сервера", "Доступ и игроки", "multiline"),
    "ChatStreams": ("Каналы чата", "Чат", "list"),
    "AutoCreateUserInWhiteList": (
        "Добавлять новых игроков в белый список",
        "Доступ и игроки",
        "boolean",
    ),
    "AllowNonAsciiUsername": (
        "Разрешить имена с национальными символами",
        "Доступ и игроки",
        "boolean",
    ),
    "MaxAccountsPerUser": ("Лимит аккаунтов пользователя", "Доступ и игроки", "integer"),
    "AllowCoop": ("Разрешить совместную игру", "Доступ и игроки", "boolean"),
    "PauseEmpty": ("Пауза пустого сервера", "Сохранение мира", "boolean"),
    "SleepAllowed": ("Разрешить сон", "Сохранение мира", "boolean"),
    "SleepNeeded": ("Необходимость сна", "Сохранение мира", "boolean"),
    "PVPLogToolHits": ("Журнал PvP-ударов инструментами", "PvP", "boolean"),
    "PVPLogFirearmHits": ("Журнал PvP-выстрелов", "PvP", "boolean"),
    "PVPMeleeWhileHitReaction": ("Удары во время реакции на попадание", "PvP", "boolean"),
    "PVPMeleeDamageModifier": ("Урон ближнего боя PvP", "PvP", "double"),
    "PVPFirearmDamageModifier": ("Урон огнестрельного оружия PvP", "PvP", "double"),
    "SafetyToggleTimer": ("Время переключения безопасности", "PvP", "integer"),
    "SafetyCooldownTimer": ("Ожидание после переключения безопасности", "PvP", "integer"),
    "SafehouseAllowFire": ("Огонь в безопасных домах", "Безопасные дома", "boolean"),
    "SafehouseAllowLoot": ("Добыча в безопасных домах", "Безопасные дома", "boolean"),
    "SafehouseAllowRespawn": ("Возрождение в безопасных домах", "Безопасные дома", "boolean"),
    "SafehouseDaySurvivedToClaim": ("Дни выживания для захвата дома", "Безопасные дома", "integer"),
    "SafeHouseRemovalTime": ("Срок хранения безопасного дома", "Безопасные дома", "integer"),
    "SafehouseMaxPlayers": ("Лимит игроков безопасного дома", "Безопасные дома", "integer"),
    "AdminSafehouse": ("Безопасные дома администраторов", "Безопасные дома", "boolean"),
    "SafehouseAllowNonResidential": (
        "Безопасные дома в нежилых зданиях",
        "Безопасные дома",
        "boolean",
    ),
    "VoiceEnable": ("Голосовой чат", "Чат", "boolean"),
    "VoiceMinDistance": ("Минимальная дальность голоса", "Чат", "double"),
    "VoiceMaxDistance": ("Максимальная дальность голоса", "Чат", "double"),
    "Voice3D": ("Объёмный голосовой чат", "Чат", "boolean"),
    "PingLimit": ("Максимальный пинг", "Сеть", "integer"),
    "UPnP": ("Автоматическая настройка портов", "Сеть", "boolean"),
    "SteamVAC": ("Steam VAC", "Сеть", "boolean"),
    "SteamScoreboard": ("Таблица игроков Steam", "Сеть", "boolean"),
    "BackupsCount": ("Количество бэкапов PZ", "Сохранение мира", "integer"),
    "BackupsOnStart": ("Бэкап PZ при запуске", "Сохранение мира", "boolean"),
    "BackupsOnVersionChange": ("Бэкап PZ при смене версии", "Сохранение мира", "boolean"),
    "BackupsPeriod": ("Период бэкапов PZ", "Сохранение мира", "integer"),
}


def comments_before(text, position, marker):
    """Only consecutive comment lines directly attached to this field count."""
    start = text.rfind("\n", 0, position) + 1
    if marker == "--" and text[start:position].strip():
        return []
    lines = text[:start].splitlines()
    result = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped and not result:
            continue
        if not stripped.startswith(marker):
            break
        result.append(stripped[len(marker) :].strip())
    return list(reversed(result))


def comment_metadata(lines, value):
    result = {}
    text = " ".join(lines)
    numeric = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    for name in ("min", "max"):
        found = re.search(rf"\b{name}:\s*({numeric})", text, re.I)
        if found:
            number = float(found[1])
            if math.isfinite(number):
                result[name] = int(number) if number.is_integer() else number
    choices = []
    for line in lines:
        match = re.fullmatch(r"(\d+)\s*=\s*(.+)", line)
        if match:
            choices.append({"value": int(match[1]), "label": match[2][:300]})
    if choices and len({c["value"] for c in choices}) == len(choices):
        result.update(type="enum", choices=choices)
    elif result and not isinstance(value, bool):
        decimal = (
            isinstance(value, float)
            or isinstance(value, str)
            and re.fullmatch(numeric, value.strip())
            and any(c in value.lower() for c in ".e")
        )
        result["type"] = (
            "double"
            if decimal or any(isinstance(result.get(p), float) for p in ("min", "max"))
            else "integer"
        )
    if "min" in result and "max" in result and result["min"] > result["max"]:
        return {}
    return result


@lru_cache(maxsize=32)
def catalog(ini, sandbox, version):
    """Pin parsed metadata to the original profile and concrete B42 version."""
    fingerprint = hashlib.sha256((version + "\0" + ini + "\0" + sandbox).encode()).hexdigest()
    result = {
        "id": fingerprint,
        "version": version,
        "source": "profile-comments",
        "ini": {},
        "sandbox": {},
    }
    if not re.fullmatch(r"42\.\d+(?:\.\d+)?", str(version or "")):
        return result
    for key, entry in ini_entries(ini).items():
        if not SECRET_KEY.search(key):
            result["ini"][key] = comment_metadata(
                comments_before(ini, entry["start"], "#"), entry["value"]
            )
    try:
        lua = LuaTable(sandbox) if sandbox else None
    except FormatError:
        lua = None
    if lua:
        for path, entry in lua.values.items():
            key = ".".join(path)
            if key != "VERSION" and not SECRET_KEY.search(key):
                result["sandbox"][key] = comment_metadata(
                    comments_before(sandbox, entry["entryStart"], "--"), entry["value"]
                )
    return result


def inferred(value):
    if isinstance(value, bool) or str(value).lower() in ("true", "false"):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "double"
    return "string"


def field(key, value, sandbox=False, custom=None, schema=None, translations=None):
    rec = {
        "key": key,
        "value": value,
        "type": inferred(value),
        "label": key,
        "group": "Дополнительные параметры",
        "hint": "Параметр вне каталога: ограничения не определены",
    }
    if sandbox:
        parent, _, name = key.rpartition(".")
        stock_type = API_INVENTORY.get(parent, {}).get(name.casefold())
        # Java field names in the API differ from serialized Lua option names.
        if not stock_type and parent == "MultiplierConfig":
            stock_type = API_INVENTORY[parent].get("xpmultiplier" + name.casefold())
        if key == "ZombieConfig.ZombiesCountBeforeDelete":
            stock_type = API_INVENTORY["ZombieConfig"].get("zombiescountbeforedeletion")
        if stock_type:
            rec.update(stock=True, type=stock_type, group="Другие настройки мира")
        if key in WORLD_LABELS:
            label, group = WORLD_LABELS[key]
            rec.update(
                label=label,
                group=group,
                stock=True,
                hint="Применение к существующему миру зависит от настройки",
            )
        rec.update(SANDBOX.get(key, {}))
        if key in SANDBOX:
            rec["stock"] = True
        if key not in SANDBOX and key not in WORLD_LABELS:
            prefix = key.split(".")[0]
            rec["group"] = next(
                (
                    group
                    for fragment, group in (
                        ("Zombie", "Зомби"),
                        ("Loot", "Добыча"),
                        ("Animal", "Животные"),
                        ("Car", "Транспорт"),
                        ("Vehicle", "Транспорт"),
                        ("Plant", "Природа"),
                        ("Map", "Карта и интерфейс"),
                        ("MultiplierConfig", "Персонажи"),
                    )
                    if fragment in prefix
                ),
                "Другие настройки мира"
                if rec.get("stock")
                else "Неизвестные / сохранённые параметры",
            )
        if "choices" in rec:
            rec["type"] = "enum"
            rec["choices"] = [
                {"value": i + 1, "label": label} for i, label in enumerate(rec["choices"])
            ]
        if custom:
            rec.update(custom)
            rec["custom"] = True
    elif key in INI:
        label, group, kind, minimum, maximum = INI[key]
        rec.update(label=label, group=group, type=kind, hint="Применяется после запуска сервера")
        if minimum is not None:
            rec["min"] = minimum
        if maximum is not None:
            rec["max"] = maximum
    if not sandbox and key in INI_PRESENTATION:
        label, group, kind = INI_PRESENTATION[key]
        rec.update(label=label, group=group, type=kind, hint="Применяется после запуска сервера")
        if kind == "list":
            rec["delimiter"] = ","
        if kind == "multiline":
            rec["lineSeparator"] = "<LINE>" if key == "ServerWelcomeMessage" else "\\n"
    if schema:
        metadata = schema["sandbox" if sandbox else "ini"].get(key, {})
        if metadata:
            rec.update(metadata)
            rec.update(schemaVersion=schema["version"], metadataSource="profile-comments")
            rec["hint"] = "Диапазон и варианты из исходной конфигурации " + schema["version"]
            if sandbox and "." not in key:
                rec["stock"] = True
                if rec["group"] == "Неизвестные / сохранённые параметры":
                    rec["group"] = "Другие настройки мира"
    if sandbox and translations and not custom:
        aliases = {
            "Zombies": "ZombieCount",
            "Distribution": "ZombieDistribution",
            "ZombieLore.Speed": "ZombieSpeed",
            "ZombieLore.Strength": "ZombieStrength",
            "ZombieLore.Toughness": "ZombieToughness",
        }
        candidates = [
            "Sandbox_" + key.replace(".", "_"),
            "Sandbox_" + key.replace(".", ""),
            "Sandbox_" + aliases.get(key, key),
            "Sandbox_" + key.split(".")[-1],
        ]
        for candidate in candidates:
            if candidate in translations:
                rec["label"] = translations[candidate]
                rec["labelSource"] = "installed-game"
                rec["hint"] = translations.get(candidate + "_tooltip", rec["hint"])
                break
    if rec["type"] == "boolean" and isinstance(value, str):
        rec["value"] = value.lower() == "true"
    if sandbox and rec.get("newWorld"):
        rec["hint"] = "Начальные условия: существующий мир может не измениться"
    return rec
