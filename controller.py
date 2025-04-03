import datetime
import json

import oath2_honeywell


def is_x_minutes_in_future(time_str, minutes_in_future, now=None):
    # Returns whether time_str is at least minutes_in_future minutes in the future from now
    if now is None:
        now = datetime.datetime.now()
    given_time = datetime.datetime.strptime(time_str, "%H:%M:%S").time()
    given_datetime = datetime.datetime.combine(now.date(), given_time)
    return given_datetime > now + datetime.timedelta(minutes=minutes_in_future)


def round_up_to_quarter_hour(dt):
    if dt.second == 0 and dt.microsecond == 0 and dt.minute % 15 == 0:
        # If already on a 15-minute increment, we're done
        return dt
    
    next_minute = ((dt.minute // 15) + 1) * 15 % 60

    if next_minute == 0:
        next_hour = dt.hour + 1

        if next_hour == 24:
            next_hour = 0
            dt += datetime.timedelta(days=1)

        return dt.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    else:
        return dt.replace(minute=next_minute, second=0, microsecond=0)


def update_heat_values(values, temperatures, device_units):
    return update_values(values, temperatures, device_units, "Heat", "heatSetpoint")

def update_cool_values(values, temperatures, device_units):
    return update_values(values, temperatures, device_units, "Cool", "coolSetpoint")

def update_values(values, temperatures, device_units, mode, setpoint):
    values_changed = False

    if values["mode"] != mode:
        values["mode"] = mode
        values_changed = True

    if "heatCoolMode" in values and values["heatCoolMode"] != mode:
        values["heatCoolMode"] = mode
        values_changed = True

    if values[setpoint] != temperatures["preferred"][device_units]:
        values[setpoint] = temperatures["preferred"][device_units]
        values_changed = True

    if values_changed:
        now = datetime.datetime.now()

        if (
                    values["thermostatSetpointStatus"] != "HoldUntil" or 
                    not is_x_minutes_in_future(values["nextPeriodTime"], 15, now)
                ):
            values["thermostatSetpointStatus"] = "HoldUntil"
            values["nextPeriodTime"] = round_up_to_quarter_hour(now + datetime.timedelta(minutes=15)).strftime("%H:%M:%S")

            return values
    
    return None


def create_empty_config(fpath):
    config = {
        "redirect_port": 8080,
        "client_id": "",
        "client_secret": "",
        "credentials_fpath": "",
        "location_prefs": {},
    }

    with open(fpath, "w") as fid:
        json.dump(config, fid, indent=4)


def load_config(fpath, check_well_formed=True):
    with open(fpath, "r") as fid:
        config = json.load(fid)
    
    if check_well_formed:
        assert "redirect_port" in config, "Missing redirect_port in config"
        assert "client_id" in config, "Missing client_id in config"
        assert "client_secret" in config, "Missing client_secret in config"
        assert "credentials_fpath" in config, "Missing credentials_fpath in config"
        assert "location_prefs" in config, "Missing location_prefs in config"

        for loc_id in config["location_prefs"]:
            loc = config["location_prefs"][loc_id]

            for dev_id in loc:
                device = loc[dev_id]
                assert "temperatures" in device, f"Missing temperatures in device {dev_id}"

                for setting in ["lowest", "preferred", "highest"]:
                    assert setting in device["temperatures"], f"Missing {setting} temperature in device {dev_id}"
                    assert "Fahrenheit" in device["temperatures"][setting] or "Celsius" in device["temperatures"][setting], f"Missing temperature unit in device {dev_id}"

                    if "Fahrenheit" in device["temperatures"][setting]:
                        assert isinstance(device["temperatures"][setting]["Fahrenheit"], (int, float)), f"Invalid temperature value in device {dev_id}"
                    
                    if "Celsius" in device["temperatures"][setting]:
                        assert isinstance(device["temperatures"][setting]["Celsius"], (int, float)), f"Invalid temperature value in device {dev_id}"
    return config


def add_missing_temperature_units(config):
    for loc_id in config["location_prefs"]:
        loc = config["location_prefs"][loc_id]

        for dev_id in loc:
            device = loc[dev_id]

            for setting in ["lowest", "preferred", "highest"]:

                if "Fahrenheit" not in device["temperatures"][setting]:
                    # Convert Celsius to Fahrenheit, rounded to nearest degree
                    fahrenheit = device["temperatures"][setting]["Celsius"] * 9 / 5 + 32
                    fahrenheit = round(fahrenheit)
                    device["temperatures"][setting]["Fahrenheit"] = fahrenheit
                
                if "Celsius" not in device["temperatures"][setting]:
                    # Convert Fahrenheit to Celsius, rounded to nearest half degree
                    celsius = (device["temperatures"][setting]["Fahrenheit"] - 32) * 5 / 9
                    celsius = round(2 * celsius) / 2
                    device["temperatures"][setting]["Celsius"] = celsius
    return config


def main(config_fpath):
    config = load_config(config_fpath)
    creds = oath2_honeywell.load_credentials(config["credentials_fpath"])

    add_missing_temperature_units(config)
    location_prefs = config["location_prefs"]

    locations_found = {
        loc_id : False for loc_id in location_prefs
    }
    devices_found = {
        loc_id : { _:False for _ in location_prefs[loc_id] } 
        for loc_id in location_prefs
    }

    get_locs_and_devs = lambda cfg: oath2_honeywell.get_locations_and_devices(
        cfg["client_id"], creds["access_token"])
    success, location_response = get_locs_and_devs(config)

    if not success:
        success, response = oath2_honeywell.refresh_access_token(
            config["client_id"], config["client_secret"], creds["refresh_token"])

        if not success:
            print("Failed to refresh access token and get locations and devices.")
            return 1
        
        success, location_response = get_locs_and_devs(config)

        if not success:
            print("Failed to get locations and devices.")
            return 2

    try:
        for location in location_response:
            loc_id = str(location["locationID"])

            if loc_id in location_prefs:
                locations_found[loc_id] = True
                print(f"Location {loc_id} found.")

                for device in location["devices"]:                
                    dev_id = device["deviceID"]

                    if dev_id in location_prefs[loc_id]:
                        devices_found[loc_id][dev_id] = True
                        print(f"Device {dev_id} found.")

                        values = device["changeableValues"]
                        units = device["units"]
                        
                        if device["indoorTemperature"] < location_prefs[loc_id][dev_id]["temperatures"]["lowest"][units]:
                            print(f"Indoor temperature ({device['indoorTemperature']} {units}) is lower than lowest temperature ({location_prefs[loc_id][dev_id]['temperatures']['lowest'][units]} {units}), updating values...")
                            values = update_heat_values(values, location_prefs[loc_id][dev_id]["temperatures"], units)

                        elif device["indoorTemperature"] > location_prefs[loc_id][dev_id]["temperatures"]["highest"][units]:
                            print(f"Indoor temperature ({device['indoorTemperature']} {units}) is higher than highest temperature ({location_prefs[loc_id][dev_id]['temperatures']['highest'][units]} {units}), updating values...")
                            values = update_cool_values(values, location_prefs[loc_id][dev_id]["temperatures"], units)

                        else:
                            print(f"Indoor temperature ({device['indoorTemperature']}) is within range, no update needed.")
                            values = None

                        if values is not None:
                            print("Going to attempt to update device settings:")
                            print(f"  Client ID: {config['client_id']}")
                            print(f"  Access Token: {creds['access_token']}")
                            print(f"  Location ID: {loc_id}")
                            print(f"  Device ID: {dev_id}")
                            print(f"  Values: {values}")

                            post_dev_settings = lambda cfg : oath2_honeywell.post_device_settings(
                                cfg["client_id"], creds["access_token"], loc_id, dev_id, values)
                            success, response = post_dev_settings(config)

                            if not success:
                                success, response = oath2_honeywell.refresh_access_token(
                                    config["client_id"], config["client_secret"], creds["refresh_token"])

                                if not success:
                                    print("Failed to refresh access token and update device settings.")
                                    return 3

                                success, response = post_dev_settings(config)

                                if not success:
                                    print("Failed to update device settings.")
                                    return 4

                            print("Success. Device settings updated.")
                            print(f"response: {response}")
    except Exception as e:
        print(f"Error: {e}")

    for loc_id, locations_found in locations_found.items():
        print(f"Location {loc_id}{"" if locations_found else " NOT"} found.")

    for loc_id, devices_found in devices_found.items():
        for device_id, found in devices_found.items():
            print(f"Device {device_id}{"" if found else " NOT"} found in location {loc_id}.")
    
    return 0


if __name__ == "__main__":
    print("Running main...")
    code = main("config.json")
    print(f"Main returned {code}")
