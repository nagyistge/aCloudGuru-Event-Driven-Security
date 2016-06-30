import json
import gzip
import base64
from StringIO import StringIO
import sets
import urllib2
import boto3
from netaddr import IPNetwork, IPAddress

#
# User variables:
#
dryrun = False
allowAWS = True
exceptions = [ 
	{"cidr": "0.0.0.0/0", "port": "123"}   
]
snsArn = "arn:aws:sns:us-east-1:012345678901:instanceKiller"
#
# Built in variables
#
aws_data_source_url = 'https://ip-ranges.amazonaws.com/ip-ranges.json'
aws_service = "AMAZON"
aws_ports = ["80", "443"]

def getInstanceForEniId(eniId):
	ec2 = boto3.resource('ec2')
	try:
		network_interface = ec2.NetworkInterface(eniId)
		return network_interface.attachment['InstanceId']
	except:
		return False

def parseEvent(event):
	# get CloudWatch logs
	data = str(event['awslogs']['data'])
	# decode and uncompress CloudWatch logs
	logs = gzip.GzipFile(fileobj=StringIO(data.decode('base64', 'strict'))).read()
	# convert the log data from JSON into a dictionary
	return json.loads(logs)

def checkForException( dstaddr, dstport ):
	for exception in exceptions:
		if  (( IPAddress(dstaddr) in IPNetwork(exception['cidr']) )
			and
			( dstport == exception['port'] )):
			print("LOG: Allowed within exception cidr {} and port {}".format( exception['cidr'], exception['port']))
			return True
	return False

def addAWSExceptions():
	
	print("LOG: Adding AWS endpoints to exceptions list.")
	
	data = urllib2.urlopen(aws_data_source_url)
	ipRanges = json.load(data)

	for range in ipRanges['prefixes']:
		if range['service'] == aws_service:
			for port in aws_ports:
				part = {}
				part['cidr'] = range['ip_prefix']
				part['port'] = port
				exceptions.append(part)

def killInstance( instanceId ):

	print("LOG: Killing instance {}".format( instanceId ))
	
	try:
		ec2 = boto3.resource('ec2')
		instance = ec2.Instance(instanceId)
	except:
		print("ERROR: Unable to find instance to kill. {}".format(instanceId))
		return False

	#Stop instance
	print("LOG: Sending stop message to instance. {}".format(instanceId))
	try:
		response = instance.stop(
			DryRun=dryrun,
			Force=True
		)
	except:
		print("ERROR: Unable to stop instance. {}".format(instanceId))
		return False

	#Snapshot volumes
	volume_iterator = instance.volumes.all()
	for volume in volume_iterator:
		snapshot = snapShotInstance( volume.id, instanceId )
		if snapshot:
			print("LOG: Snapshot for instance {} volume {} snapshot {}".format( instanceId, volume.id, snapshot ))
		else:
			print("WARNING: Unable to snapshot for instance {} volume {}".format( instanceId, volume.id ))

	#Terminate instance
	print("LOG: Sending terminate message to instance. {}".format(instanceId))
	try:
		response = instance.terminate(
			DryRun=dryrun
		)
	except:
		print("ERROR: Unable to terminate instance to kill. {}".format(instanceId))
		return False
		
	sendNotification( instanceId, snapshot )

def snapShotInstance( volumeId, instanceId ):

	try:
		ec2 = boto3.resource('ec2')
		volume = ec2.Volume(volumeId)
	except:
		print("ERROR: Unable to find volume {} to snapshot".format(volumeId))
		return False
	
	snapshot = volume.create_snapshot(
		DryRun=dryrun,
		Description="Snapshot for instance {} made by the instanceKiller.".format(instanceId)
	)

	return snapshot.id
	

def sendNotification( instanceId, snapshotId ):
	
	client = boto3.client('sns')
	
	try:
		response = client.publish(
			TopicArn=snsArn,
			Message="Instance {} has been terminated.  Snapshot {} created.".format( instanceId, snapshotId  ),
			Subject='InstanceKiller has terminated an instance'
		)
		print("LOG: SNS Notification sent.")
	except:
		print("ERROR: Unable to send SNS notification.")	
	

def lambda_handler(event, context):

	# Print out the event, helps with debugging
	print(event)

	events = parseEvent(event)
	#print(events)

	if allowAWS:
		addAWSExceptions()

	#print(exceptions)

	killList = set()
	unknownInterfaces = []

	for record in events['logEvents']:
	
		try:
			extractedFields = record['extractedFields']
		except:
			raise Exception("ERROR: Could not find 'extractedFields' is the CloudWatch feed set correctly?")
			return False
		
		instanceId = getInstanceForEniId(extractedFields['interface_id'])
		
		if instanceId:
		
			print("LOG: Instance:{}\t Interface:{}\t SrcAddr:{}\t DstAddr:{}\t DstPort:{}\t".format( 
				instanceId,
				extractedFields['interface_id'],
				extractedFields['srcaddr'],
				extractedFields['dstaddr'],
				extractedFields['dstport']
			))
			
			if checkForException( extractedFields['dstaddr'], extractedFields['dstport'] ):
				print("LOG: OK")
				True
			else:
				print("LOG: ALERT!! Disallowed traffic {}:{} by instance {}".format( extractedFields['dstaddr'], extractedFields['dstport'],  instanceId ))
				killList.add(instanceId)
				
		else:
			unknownInterfaces.append(extractedFields['interface_id'])

	print("LOG: There are {} instances on the kill list!".format( len(killList) ))
	
	if len(unknownInterfaces):
		print("LOG: Found {} interfaces not attached to instances (probably an ELB).".format( len(unknownInterfaces) ))
		print("LOG: Interfaces without instances:{}".format(unknownInterfaces))
	
	killed = 0
	
	for instanceId in killList:
		if killInstance( instanceId ):
			killed = killed + 1

	return ("Killed {} instances.".format( killed ))
	