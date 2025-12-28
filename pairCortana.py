import os
import re
import time
import subprocess
import requests
import json
import html
import socket
import urllib3

CERT = False
urllib3.disable_warnings()

# Hide ssl errors 

IP = '192.168.43.1'
DOMAIN = f'https://{IP}'

def parseTable(output):
    lines = output.splitlines()
    headerLine = f" {lines[0]}"
    lines = lines[1:]

    headerIndexs = []

    for header in headerLine.split():
        headerIndexs.append({
            'header': header,
            'index': headerLine.find(f' {header}')
        })

    networks = []
    for network in lines:
        networkData = {}
        for cur, nex in zip(headerIndexs, headerIndexs[1:]):
            networkData[cur['header']] = network[cur['index']:nex['index']].strip()
        networkData[headerIndexs[-1]['header']] = network[headerIndexs[-1]['index']:].strip()
        networks.append(networkData)
    return networks

def scanWifi():
    # Use the command line to scan for WiFi networks
    result = subprocess.run(['nmcli', 'device', 'wifi', 'list'], capture_output=True, text=True)
    
    # Check if the command was successful
    if result.returncode != 0:
        print("Error scanning for WiFi networks.")
        return
    
    # Print the output of the command
    return parseTable(result.stdout)

def connectWifi(network):
    if not network:
        return False
    
    # Connect to the invoke network
    print(f"Connecting to {network['SSID']}...")
    subprocess.run(['nmcli', 'dev', 'wifi', 'connect', network['SSID']])

    # Wait for the connection to be established
    time.sleep(5)
    # Check if connected
    networks = scanWifi()
    newNetworks = next((network for network in networks if network['IN-USE'] == '*'), None)
    if newNetworks and newNetworks['SSID'] == network['SSID']:
        print(f"Connected to {newNetworks['SSID']}")
        return True
    else:
        print("Failed to connect to invoke network.")
        return False

def connectToInvoke():
    print("Scanning for WiFi networks...")
    networks = scanWifi()
    currentNetwork = next((network for network in networks if network['IN-USE'] == '*'), None)
    invokeFound = False

    while invokeFound == False:
        invokeNetwork = next((network for network in networks if network['SSID'].startswith('HK Invoke_')), None)
        if invokeNetwork:
            print(f"Invoke network found: {invokeNetwork['SSID']}")
            invokeFound = True
        else:
            print("Waiting for HK Invoke...")
            time.sleep(5)
            networks = scanWifi()

    # Disconnect from current network
    if currentNetwork:
        print(f"Disconnecting from {currentNetwork['SSID']}...")
        subprocess.run(['nmcli', 'con', 'down', currentNetwork['SSID']])

    return connectWifi(invokeNetwork)
    
def checkStatus(ports = False):
    url = f"{DOMAIN}:12345/" if ports else f"{DOMAIN}/"
    try:
        r = requests.get(url, verify=CERT)
        return r.status_code == 200
    except:
        return False

def getMacAddress(addr):
    ping = subprocess.run(['ping', addr, '-c', '1'], capture_output=True, text=True)
    if '0% packet loss' not in ping.stdout:
        return None
    else:
        arp = subprocess.run(['arp', '-n', IP], capture_output=True, text=True)
        mac = re.search(r"(([a-f\d]{1,2}\:){5}[a-f\d]{1,2})", arp.stdout).groups()[0]
        return mac

def findInvokeIP(target_mac):
    machineAddr = getMachineIP()
    if not machineAddr:
        print('Failed to get machine IP address.')
        return None
    
    machineAddr = machineAddr.split('.')
    machineAddr = f'{machineAddr[0]}.{machineAddr[1]}.{machineAddr[2]}'
    i = 0
    while i <= 255:
        addr = f'{machineAddr}.{i}'
        if getMacAddress(addr) == target_mac:
            return addr
        i += 1

    return None

def getMachineIP():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    addr = s.getsockname()[0]
    s.close()
    return addr

def getCurrentWifiNetwork():
    networks = scanWifi()
    currentNetwork = next((network for network in networks if network['IN-USE'] == '*'), None)
    if currentNetwork:
        return currentNetwork
    else:
        return None

def main():
    currentNetwork = getCurrentWifiNetwork()

    if not connectToInvoke():
        exit(0)
    
    INVOKE_MAC = getMacAddress(IP)
    print(INVOKE_MAC)

    while not checkStatus():
        time.sleep(5)

    print('Got status from Invoke')

    authData = {
        "cid": "xxxxxxxxxxxxxxxx",
        "settings": {
            "Location": {
                "Longitude": -122.1279453,
                "Latitude": 47.6423012
            },
            "TimeZone": "Central Standard Time",
            "IANATimeZone": "America/Chicago",
            "FriendlyName": "Jemma's Invoke",
            "StreetAddress": "15835 NE 36th St, Redmond, WA 98052"
        },
        "clientIdIndex": "1",
        "Language": "en-us",
        "transfer_token": "M.C558_BAY.2.U.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }

    r = requests.post(f"{DOMAIN}:12345/token_confirm", verify=CERT)
    if r.status_code == 200:
        print("Successfully confirmed token.")

    r = requests.post(f"{DOMAIN}:12345/", json=authData, verify=CERT)

    if r.status_code == 200:
        print("Successfully sent json to Invoke.")
    else:
        print(f"Failed to send data to Invoke. Status code: {r.status_code}")
        print(r.text)
        exit(0)

    # Get wifi networks from the Invoke
    print("Getting wifi networks from Invoke...")
    r = requests.get(f'{DOMAIN}/scanresult.asp', verify=CERT)
    if r.status_code != 200:
        print(f"Failed to get wifi networks. Status code: {r.status_code}")
        exit(0)
    
    networks = json.loads(r.text)
    print("Wifi networks from Invoke:")
    index = 1
    for network in networks['Items']:
        print(f"ID: {index}, SSID: {html.unescape(network['SSID'])}, Signal Strength: {network['rssi']}, Security: {network['Security']}, band: {network['band']}")
        index += 1
    
    NetworkIndex = input("Enter the ID of the network you want to connect to: ")
    NetworkIndex = int(NetworkIndex) - 1
    if NetworkIndex < 0 or NetworkIndex >= len(networks['Items']):
        print("Invalid network ID.")
        exit(0)
    network = networks['Items'][NetworkIndex]

    # Enter the network password
    password = input("Enter the network password: ")
    if not password:
        print("No password entered.")
        exit(0)
    # Connect to the network
    print(f"Connecting to {html.unescape(network['SSID'])}...")
    networkName = html.unescape(network['SSID'])

    checkStatus()

    print(networkName)
    print(password)

    headers = {'Content-type': 'application/x-www-form-urlencoded', 'accept-encoding': 'gzip, deflate'}
    r = requests.post(f'{DOMAIN}/goform/HandleSACConfiguration', data=f"SSID={networkName}&Passphrase={password}&Security={network['Security']}", headers=headers, verify=CERT)
    print('Invoke Response:', r.text)

    if 'reconnect' in r.text:
        r = requests.get(f'{DOMAIN}/goform/HandleCommand?CMD=RECONNECT_ACK', verify=CERT)
        print(r.text)

    connectToInvoke()

    checkStatus()

    checkStatus(True)
    
    r = requests.get(f'{DOMAIN}/ConnectedStatus.asp', verify=CERT)
    if r.status_code == 200 and r.text == 'connected':
        print("Successfully connected to the network.")
    else:
        print(f"Failed to connect to the network. Status code: {r.status_code}")
        exit(-1)
    
    print(f'Reconnect to {currentNetwork['SSID']}')
    if not connectWifi(currentNetwork):
        print('Failed to connect to main network')
        exit(-1)

    while True:
        invokeIP = findInvokeIP()
        if invokeIP:
            print(f'Found Invoke! Device connected at {invokeIP}')
            break
    

if __name__ == "__main__":
    main()