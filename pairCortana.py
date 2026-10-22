#!/usr/bin/python
import os
import re
import time
import subprocess
import shutil
import requests
import json
import html
import socket
import urllib3
import concurrent.futures
import platform

CERT = False
urllib3.disable_warnings()

# Hide ssl errors

IP = '192.168.43.1'
DOMAIN = f'https://{IP}'
MAC_PATTERN = r"(([0-9A-Fa-f]{1,2}[:-]){5}[0-9A-Fa-f]{1,2})"

def parseTable(output):
	lines = output.splitlines()
	headerLine = f" {lines[0]}"
	lines = lines[1:]

	headerIndexes = []

	for header in headerLine.split():
		headerIndexes.append({
			'header': header,
			'index': headerLine.find(f' {header}')
		})

	networks = []
	for network in lines:
		networkData = {}
		for cur, nex in zip(headerIndexes, headerIndexes[1:]):
			networkData[cur['header']] = network[cur['index']:nex['index']].strip()
		networkData[headerIndexes[-1]['header']] = network[headerIndexes[-1]['index']:].strip()
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
	if not networks:
		return False

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

	if not networks:
		return False

	currentNetwork = next((network for network in networks if network['IN-USE'] == '*'), None)
	invokeFound = False
	invokeNetwork = None

	while invokeFound == False:
		invokeNetwork = next((network for network in networks if network['SSID'].startswith('HK Invoke_')), None) # type: ignore
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
	pingCommands = []
	if platform.system().lower() == 'windows':
		pingCommands.append(['ping', '-n', '1', addr])
	else:
		# macOS and Linux both support -c for count.
		pingCommands.append(['ping', '-c', '1', addr])

	ping = None
	for pingCmd in pingCommands:
		try:
			ping = subprocess.run(pingCmd, capture_output=True, text=True, timeout=2)
			break
		except (subprocess.TimeoutExpired, FileNotFoundError):
			continue

	if ping is None:
		return None

	if ping.returncode != 0:
		return None

	arpCommands = [
		['ip', 'neigh', 'show', addr],
		['arp', '-n', addr],
		['arp', '-a', addr],
		['arp', '-a']
	]

	arpOutput = ''
	for arpCmd in arpCommands:
		try:
			arp = subprocess.run(arpCmd, capture_output=True, text=True, timeout=2)
		except (subprocess.TimeoutExpired, FileNotFoundError):
			continue
		if arp.stdout:
			arpOutput = arp.stdout
			break

	if not arpOutput:
		return None

	mac = re.search(MAC_PATTERN, arpOutput, re.IGNORECASE)
	if not mac:
		return None
	return normalizeMac(mac.groups()[0])

def normalizeMac(mac):
	if not mac:
		return None
	parts = mac.strip().lower().replace('-', ':').split(':')
	if len(parts) != 6:
		return None
	try:
		return ':'.join(f'{int(part, 16):02x}' for part in parts)
	except ValueError:
		return None

def macVariants(mac):
	normalized = normalizeMac(mac)
	if not normalized:
		return set()

	variants = {normalized}
	parts = normalized.split(':')
	firstOctet = int(parts[0], 16)
	# Some devices flip the "locally administered" bit between AP/client modes.
	flipped = firstOctet ^ 0x02
	variants.add(':'.join([f'{flipped:02x}'] + parts[1:]))
	return variants

def macsMatch(left, right):
	leftVariants = macVariants(left)
	rightVariants = macVariants(right)
	if not leftVariants or not rightVariants:
		return False
	return len(leftVariants.intersection(rightVariants)) > 0

def findIPInNeighborTable(target_mac, interface = None):
	target_mac = normalizeMac(target_mac)
	if not target_mac:
		return None
	targetMacVariants = macVariants(target_mac)

	neighborCommands = []
	if interface:
		neighborCommands.append(['ip', 'neigh', 'show', 'dev', interface])
	neighborCommands.extend([
		['ip', 'neigh', 'show'],
		['arp', '-an'],
		['arp', '-a']
	])

	for cmd in neighborCommands:
		try:
			result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
		except (subprocess.TimeoutExpired, FileNotFoundError):
			continue

		if not result.stdout:
			continue

		for line in result.stdout.splitlines():
			ipMatch = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
			macMatch = re.search(MAC_PATTERN, line, re.IGNORECASE)
			if not ipMatch or not macMatch:
				continue
			normalized = normalizeMac(macMatch.group(1))
			if normalized and normalized in targetMacVariants:
				return ipMatch.group(1)

	return None

def scanWithArpScan(target_mac):
	target_mac = normalizeMac(target_mac)
	if not target_mac:
		return None

	arpScanPath = shutil.which('arp-scan')
	if not arpScanPath:
		return None

	try:
		result = subprocess.run([arpScanPath, '--localnet', '--ignoredups'], capture_output=True, text=True, timeout=12)
	except subprocess.TimeoutExpired:
		return None

	if result.returncode != 0:
		stderr = result.stderr.strip()
		if stderr:
			print(f'arp-scan failed: {stderr}')
		return None

	for line in result.stdout.splitlines():
		match = re.search(rf"^(\d+\.\d+\.\d+\.\d+)\s+{MAC_PATTERN}", line, re.IGNORECASE)
		if match and macsMatch(match.group(2), target_mac):
			return match.group(1)

	return None

def probeAddressForMac(addr):
	return addr, getMacAddress(addr)

def getLocalNetworkContext():
	try:
		route = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True, text=True, timeout=2)
		line = route.stdout.strip()
		if route.returncode == 0 and line:
			devMatch = re.search(r"\bdev\s+(\S+)", line)
			srcMatch = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", line)
			if srcMatch:
				srcIP = srcMatch.group(1)
				parts = srcIP.split('.')
				subnetPrefix = f'{parts[0]}.{parts[1]}.{parts[2]}'
				return {
					'interface': devMatch.group(1) if devMatch else None,
					'src_ip': srcIP,
					'subnet_prefix': subnetPrefix
				}
	except (subprocess.TimeoutExpired, FileNotFoundError):
		pass

	machineAddr = getMachineIP()
	if not machineAddr:
		return None
	parts = machineAddr.split('.')
	return {
		'interface': None,
		'src_ip': machineAddr,
		'subnet_prefix': f'{parts[0]}.{parts[1]}.{parts[2]}'
	}

def findInvokeIP(target_mac, deadline = None):
	if not target_mac:
		return None
	target_mac = normalizeMac(target_mac)
	if not target_mac:
		print('Invoke MAC is invalid.')
		return None
	print(f'Looking for Invoke MAC: {target_mac} (accepted variants: {sorted(macVariants(target_mac))})')

	networkContext = getLocalNetworkContext()
	if not networkContext:
		print('Failed to get machine IP address.')
		return None

	interface = networkContext['interface']
	subnetPrefix = networkContext['subnet_prefix']
	print(f"Local network context: interface={interface}, ip={networkContext['src_ip']}, subnet={subnetPrefix}.0/24")

	cachedIP = findIPInNeighborTable(target_mac, interface = interface)
	if cachedIP:
		if not cachedIP.startswith(f'{subnetPrefix}.'):
			print(f'Ignoring cached Invoke IP from another subnet: {cachedIP}')
		else:
			print(f'Found Invoke in neighbor table: {cachedIP}')
			return cachedIP

	arpScanIP = scanWithArpScan(target_mac)
	if arpScanIP:
		if not arpScanIP.startswith(f'{subnetPrefix}.'):
			print(f'Ignoring arp-scan Invoke IP from another subnet: {arpScanIP}')
		else:
			print(f'Found Invoke via arp-scan: {arpScanIP}')
			return arpScanIP

	print(f'Scanning subnet {subnetPrefix}.0/24 for Invoke MAC {target_mac}...')
	addresses = [f'{subnetPrefix}.{i}' for i in range(1, 255)]
	with concurrent.futures.ThreadPoolExecutor(max_workers = 48) as executor:
		futureMap = {executor.submit(probeAddressForMac, addr): addr for addr in addresses}
		for index, future in enumerate(concurrent.futures.as_completed(futureMap), start = 1):
			if deadline and time.time() >= deadline:
				print('Invoke IP search deadline reached during subnet scan.')
				return None
			if index % 32 == 0:
				print(f'  - probes completed: {index}/{len(addresses)}')
			try:
				addr, mac = future.result()
			except Exception:
				continue
			if macsMatch(mac, target_mac):
				return addr

	return None

def getMachineIP():
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	s.connect(("8.8.8.8", 80))
	addr = s.getsockname()[0]
	s.close()
	return addr

def getCurrentWifiNetwork():
	networks = scanWifi()
	if not networks:
		return None
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

	#NetworkIndex = input("Enter the ID of the network you want to connect to: ")
	NetworkIndex = 0 #int(NetworkIndex) - 1
	if NetworkIndex < 0 or NetworkIndex >= len(networks['Items']):
		print("Invalid network ID.")
		exit(0)
	network = networks['Items'][NetworkIndex]

	# Enter the network password
	#password = input("Enter the network password: ")
	password = "FuckElonMusk69!"
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

	if currentNetwork:
		print(f"Reconnect to {currentNetwork['SSID']}")
		if not connectWifi(currentNetwork):
			print('Failed to connect to main network')
			exit(-1)

	# Give Invoke time to leave AP mode and join the selected network.
	time.sleep(3)

	print('Looking for Invoke on local network...')
	lookupTimeoutSeconds = 60
	startTime = time.time()
	deadline = startTime + lookupTimeoutSeconds
	attempt = 1
	while time.time() < deadline:
		elapsed = time.time() - startTime
		remaining = max(0.0, deadline - time.time())
		print(f'Invoke IP lookup attempt {attempt} (elapsed: {elapsed:.1f}s, remaining: {remaining:.1f}s)')
		invokeIP = findInvokeIP(INVOKE_MAC, deadline = deadline)
		if invokeIP:
			print(f'Found Invoke! Device connected at {invokeIP}')
			break
		attempt += 1
		time.sleep(1)
	else:
		print('Timed out after 60 seconds while searching for Invoke IP.')
		exit(-1)

if __name__ == "__main__":
	main()