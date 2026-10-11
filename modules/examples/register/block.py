from pioneer.block import Node,Param,In,Out,Event,Number,node
from driver import Register

@node(id='read',title='Регистр I2C: прочитать',category='Учебные модули')
class Read(Node):
    interface=Param('interface','i2c1',modes=('i2c',),required=True,title='Шина')
    addr=Param(int,0x48,min=3,max=119,title='Адрес')
    when=In(Event,title='Прочитать')
    value=Out(Number,title='Значение')
    def setup(self):self.device=Register(self.hw.i2c(self.interface,self.addr))
    def start(self):self.value.set(self.device.read())
    def on_event(self,name,payload):self.value.set(self.device.read())
