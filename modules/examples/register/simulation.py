class Simulation:
    def __init__(self,config):self.value=int(config.get('value',1234))
    def i2c_write(self,bus,addr,data):pass
    def i2c_read(self,bus,addr,n):return self.value.to_bytes(2,'big').hex()[:n*2]
    def action(self,name,payload):
        if name!='set':raise ValueError('unknown action')
        value=int(payload['value'])
        if not 0<=value<=65535:raise ValueError('value 0…65535')
        self.value=value
    def state(self):return {'value':self.value}
