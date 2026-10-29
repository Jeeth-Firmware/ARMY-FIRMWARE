import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

cred = credentials.Certificate('./driver_app.json')
firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection('car').document('1')

def updateDB(speed, status):
    if status:
        doc_ref.update({'speed': speed, 'driverStatus': status})

def updateDBNew(lat, long, speed):
    if 0 <= speed <= 300:
        doc_ref.update({'speed': speed})
    if lat != 0 and long != 0:
        doc_ref.update({'lat': lat, 'long': long})

def updateDriverStatus(status):
    doc_ref.update({'driverStatus': status or 'Normal'})

