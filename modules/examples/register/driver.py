class Register:
    def __init__(self,device):self.device=device
    def read(self):return int.from_bytes(self.device.write_read(b'\x00',2),'big')
