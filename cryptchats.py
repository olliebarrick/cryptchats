#!/usr/bin/python2
import curve25519
import hkdf
import libnacl
from libnacl import crypto_onetimeauth as poly1305

from os import urandom
from random import SystemRandom
import struct

rand = SystemRandom()

class ChatsError(Exception):
    pass

class Chats(object):
    def __init__(self, long_term, bob_long_term, max_length=480, chaff_block_size=16,
      debug=False):
        self.debug = debug

        self.proto_id = 'cryptchats-protocol-v1'

        self.long_term = long_term
        self.long_term_public = self.long_term.get_public()
        self.bob_long_term = bob_long_term

        self.send_pending = None
        self.receive_pending = None

        self.i_am_alice = False

        # this gets the pre-base64 length, 480 bytes after base64 seems good for IRC.
        self.max_length = max_length * 3 / 4
        self.chaff_block_size = chaff_block_size
        self.cipher_nonce_size = libnacl.crypto_box_NONCEBYTES

        self.init_keys()

    def init_keys(self):
        self.initialized = False
        self.initial_key = { 'initial_key': True }
        self.send = { 'alice': curve25519.Private() }
        self.receive = { 'alice': curve25519.Private(), 'receiver': True }

    def established(self):
        return 'bob' in self.receive

    def derive_key(self, key, length=96):
        return hkdf.hkdf_expand(key, self.proto_id, length)

    def chaff(self, blocks):
        length = self.max_length / self.chaff_block_size
        needed_blocks = (length - len(blocks) * 2) / 2

        for _ in xrange(needed_blocks):
            c = [ urandom(self.chaff_block_size), urandom(self.chaff_block_size) ]
            blocks.insert(rand.randint(0, len(blocks)), c)

        c = ''
        for index, block in enumerate(blocks):
            c += block[0] + block[1]

        return c

    def get_blocks(self, _str):
        while _str:
            yield _str[:self.chaff_block_size]
            _str = _str[self.chaff_block_size:]

    def get_block_pairs(self, _str):
        blocks = [ [] ]

        for block in self.get_blocks(_str):
            if len(blocks[-1]) == 2:
                yield blocks[-1]
                blocks.append([ block ])
            else:
                blocks[-1].append(block)

        if len(blocks[-1]) == 2:
            yield blocks[-1]

    def mac_blocks(self, ct, key):
        blocks = []

        for block in self.get_blocks(ct):
            mac = poly1305(block, key)[:self.chaff_block_size]
            blocks.append([ block, mac ])

        return blocks

    def encrypt(self, pt, key, counter):
        if len(pt) % 16:
            pt += '\x00' * (16 - len(pt) % 16)

        return libnacl.crypto_secretbox(pt, counter, key)

    def decrypt(self, ct, key, counter):
        try:
            return libnacl.crypto_secretbox_open(ct, counter, key)
        except:
            return None

    def decrypt_initial_keyx(self, ct, ack=False):
        counter = ct[:self.cipher_nonce_size]
        ct = ct[self.cipher_nonce_size:]
        key = self.derive_keys()

        self.print_key('Decrypting initial key exchange.', key)
        
        if ack:
            msg_key = key['exchange_key']
        else:
            msg_key = key['message_key']

        pt = self.decrypt(ct, msg_key, counter)
        if pt:
            return pt[:32], pt[32:64]

    def decrypt_keyx(self, ct, decryptor):
        decryptor = self.derive_keys(decryptor)

        pt = self.decrypt(ct, decryptor['exchange_key'], decryptor['exchange_counter'])
        if pt:
            return pt[:32]

    def decrypt_message(self, ct, decryptor):
        decryptor = self.derive_keys(decryptor)

        pt = self.decrypt(ct, decryptor['message_key'], decryptor['message_counter'])
        if not pt:
            return None

        bob_ephemeral, msg = pt[:32], pt[32:]

        self.receive = decryptor

        # it's encrypted with the same key it advertised.. shouldn't happen.
        if bob_ephemeral == self.receive['bob'].serialize():
            raise
        # it's advertising the receive_pending key, we need to respond again because
        # they may not have gotten it.
        elif self.receive_pending \
          and bob_ephemeral == self.receive_pending['bob'].serialize():
            pass
        # yay a new key! we need to respond.
        elif not self.receive_pending \
          or bob_ephemeral != self.receive_pending['bob'].serialize():

            self.receive_pending = {
                'alice': curve25519.Private(),
                'bob': curve25519.Public(bob_ephemeral),
                'receiver': True,
                'acked': False
            }
        # uncharted territory...
        else:
            raise

        return msg

    def get_public(self, key):
        return key.get_public().serialize()

    def derive_keys(self, key=None):
        # initial key exchanges.
        if not key or 'bob' not in key:
            master = self.long_term.get_shared_key(self.bob_long_term, self.derive_key)
            key = key or {}
        # we're sending
        elif 'receiver' not in key:
            master  = self.long_term.get_shared_key(key['bob'], self.derive_key)
            master += key['alice'].get_shared_key(self.bob_long_term, self.derive_key)
            master += key['alice'].get_shared_key(key['bob'], self.derive_key)
        # we are receiving
        else:
            if 'alice' not in key:
                key['alice'] = curve25519.Private()

            master  = key['alice'].get_shared_key(self.bob_long_term, self.derive_key)
            master += self.long_term.get_shared_key(key['bob'], self.derive_key)
            master += key['alice'].get_shared_key(key['bob'], self.derive_key)

        # derive keys
        # increment counter
        if 'counter' in key:
            key['counter'] += 1
        else:
            key['counter'] = 0

        mac_key = self.proto_id + '::poly1305'
        master = poly1305(master + str(key['counter']), mac_key)
        master = self.derive_key(master, 176)
        
        keys = list(struct.unpack('>32s32s32s32s24s24s', master))
        key['exchange_counter'] = keys.pop()
        key['message_counter'] = keys.pop()
        key['exchange_chaff_key'] = keys.pop()
        key['chaff_key'] = keys.pop()
        key['exchange_key'] = keys.pop()
        key['message_key'] = keys.pop()
        return key

    def receive_key(self, bob_ephemeral):
        self.receive['bob'] = curve25519.Public(bob_ephemeral)
        if 'counter' in self.receive:
            del self.receive['counter']

    def send_key(self, bob_ephemeral):
        self.send['bob'] = curve25519.Public(bob_ephemeral)
        if 'counter' in self.send:
            del self.send['counter']

    def got_key(self, bob_ephemeral):
        if not bob_ephemeral or 'bob' not in self.send:
            return

        if self.send_pending and bob_ephemeral != self.send['bob'].serialize():
            self.send = self.send_pending
            self.send_key(bob_ephemeral)
            self.send_pending = None

    def encrypt_msg(self, msg):
        self.send = self.derive_keys(self.send)
        self.print_key('Encrypting message.', self.send)

        if not self.send_pending:
            self.send_pending = { 'alice': curve25519.Private() }

        pt  = self.get_public(self.send_pending['alice'])
        pt += msg

        # message buffering
        if 'msgs' not in self.send_pending:
            self.send_pending['msgs'] = []
        self.send_pending['msgs'].append(msg)

        if not self.established():
            return

        ct = self.encrypt(pt, self.send['message_key'],
            self.send['message_counter'])

        blocks = self.mac_blocks(ct, self.send['chaff_key'])
        return self.chaff(blocks)

    def encrypt_keyx(self):
        self.send = self.derive_keys(self.send)
        self.print_key('Encrypting keyx.', self.send)

        self.receive_pending['acked'] = True

        data = self.get_public(self.receive_pending['alice'])
        ct = self.encrypt(data, self.send['exchange_key'],
            self.send['exchange_counter'])

        blocks = self.mac_blocks(ct, self.send['exchange_chaff_key'])
        return self.chaff(blocks)

    def encrypt_initial_keyx(self):
        self.i_am_alice = not self.established()
        self.initialized = True

        counter = urandom(self.cipher_nonce_size)
        key = self.derive_keys()
        self.print_key('Encrypting initial key exchange.', key)

        if self.i_am_alice:
            ephem_keys  = self.get_public(self.receive['alice'])
            ephem_keys += self.get_public(self.send['alice'])
            msg_key = key['message_key']
            chaff_key = key['chaff_key']
        else:
            ephem_keys  = self.get_public(self.send['alice'])
            ephem_keys += self.get_public(self.receive['alice'])
            msg_key = key['exchange_key']
            chaff_key = key['exchange_chaff_key']

        ct = self.encrypt(ephem_keys, msg_key, counter)

        blocks = self.mac_blocks(counter + ct, chaff_key)
        return self.chaff(blocks)

    def try_dechaffing(self, ct):
        blocks = []
        exchange_blocks = []

        for key in [ self.receive_pending, self.receive, self.initial_key ]:
            if not key:
                continue
       
            if 'counter' not in key:
                key['counter'] = -1
            
            key = self.derive_keys(key)

            for block_pair in self.get_block_pairs(ct):
                if poly1305(block_pair[0], key['chaff_key'])[:self.chaff_block_size] \
                  == block_pair[1]:
                    blocks.append(block_pair[0])
                elif poly1305(block_pair[0],
                  key['exchange_chaff_key'])[:self.chaff_block_size] == block_pair[1]:
                    exchange_blocks.append(block_pair[0])

            if blocks or exchange_blocks:
                break
            else:
                key['counter'] -= 1

        if blocks or exchange_blocks:
            if key == self.initial_key:
                del key['counter']
            else:
                key['counter'] -= 1
            return ''.join(blocks), ''.join(exchange_blocks), key
        else:
            return None, None, None

    def decrypt_msg(self, ct):
        # self.print_key('Received message.', self.receive)
        ct, exchange_ct, key = self.try_dechaffing(ct)

        if not ct and not exchange_ct:
            raise ChatsError('not encrypted.')

        if 'bob' not in key:
            ack = False
            if exchange_ct:
                ct = exchange_ct
                ack = True

            bob_ephem1, bob_ephem2 = self.decrypt_initial_keyx(ct, ack=ack)
            if not bob_ephem1:
                print ':('
                return None

            msgs = []
            if self.send_pending and 'msgs' in self.send_pending:
                msgs = self.send_pending['msgs']
                self.send_pending['msgs'] = []

            if self.i_am_alice and ack:
                self.receive_key(bob_ephem1)
                self.send_key(bob_ephem2)
                return { 'keyx': True, 'msgs': msgs }
            elif not ack:
                self.init_keys()
                self.send_key(bob_ephem1)
                self.receive_key(bob_ephem2)
                return { 'keyx': self.encrypt_initial_keyx(), 'msgs': msgs }
            else:
                print ';o'
                return None
        elif ct:
            self.receive = key
            self.print_key('Decrypting message.', self.receive)
            msg = self.decrypt_message(ct, self.receive)

            if self.receive_pending and not self.receive_pending['acked']:
                return { 'msg': msg, 'keyx': self.encrypt_keyx() }
            else:
                return { 'msg': msg }
        else:
            self.print_key('Decrypting keyx.', key)
            bob_ephemeral = self.decrypt_keyx(exchange_ct, key)
            self.got_key(bob_ephemeral)
            return None

    def print_key(self, title, key):
        if not self.debug:
            return

        print ''
        print title
        print ''

        print 'Alice long term public: ' + self.get_public(self.long_term).encode('hex')
        print 'Bob long term public:   ' + self.bob_long_term.serialize().encode('hex')

        if 'alice' in key:
            print 'Alice ephemeral public: ' + self.get_public(key['alice']).encode('hex')
        if 'bob' in key:
            print 'Bob ephemeral public:   ' + key['bob'].serialize().encode('hex')
        if 'message_key' in key:
            print 'Shared key:             ' + key['message_key'].encode('hex')
        if 'chaff_key' in key:
            print 'Chaff key:              ' + key['chaff_key'].encode('hex')
        if 'exchange_key' in key:
            print 'Exchange shared key:    ' + key['exchange_key'].encode('hex')
        if 'exchange_chaff_key' in key:
            print 'Exchange chaff key:     ' + key['exchange_chaff_key'].encode('hex')
        if 'counter' in key:
            print 'Counter:                %d' % key['counter']

        print ''

if __name__ == "__main__":
    alice_key = curve25519.Private()
    bob_key = curve25519.Private()

    alice = Chats(alice_key, bob_key.get_public(), 400, 8, debug=False)
    bob = Chats(bob_key, alice_key.get_public(), 400, 8, debug=False)

    ct = alice.encrypt_initial_keyx()
    msg = bob.decrypt_msg(ct)
    alice.decrypt_msg(msg['keyx'])

    print '\nAlice -> Bob initial: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    if msg['keyx']: alice.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nAlice -> Bob, Bob decrypts but forgets to respond to the key exchange: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    print 'Plaintext: %s' % msg['msg']

    print '\nAlice -> Bob, Bob decrypts and responds to the key exchange: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    if msg['keyx']: alice.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nAlice -> Bob, Bob decrypts and responds to the key exchange: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    if msg['keyx']: alice.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nAlice -> Bob, Alice loses her message: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')

    print '\nAlice -> Bob, Alice sends Bob another message and Bob responds: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    if msg['keyx']: alice.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nBob -> Alice, Bob responds to Alice. She responds to the key exchange: '
    ct = bob.encrypt_msg('ayy :)')
    msg = alice.decrypt_msg(ct)
    if msg['keyx']: bob.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nAlice -> Bob, Bob decrypts and responds to the key exchange: '
    ct = alice.encrypt_msg('ayy lmaoayy lmao')
    msg = bob.decrypt_msg(ct)
    if msg['keyx']: alice.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nBob -> Alice, Alice forgets to respond to the key exchange: '
    ct = bob.encrypt_msg('pls response')
    msg = alice.decrypt_msg(ct)
    print 'Plaintext: %s' % msg['msg']

    print '\nBob -> Alice, Alice never receives the message: '
    ct = bob.encrypt_msg('pls response')

    print '\nBob -> Alice, Alice forgets to respond to the key exchange: '
    ct = bob.encrypt_msg('pls response')
    msg = alice.decrypt_msg(ct)
    print 'Plaintext: %s' % msg['msg']

    print '\nBob -> Alice, Alice finally responds to the key exchange: '
    ct = bob.encrypt_msg('pls response')
    msg = alice.decrypt_msg(ct)
    if msg['keyx']: bob.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']

    print '\nBob -> Alice, Alice responds to the key exchange: '
    ct = bob.encrypt_msg('pls response')
    msg = alice.decrypt_msg(ct)
    if msg['keyx']: bob.decrypt_msg(msg['keyx'])
    print 'Plaintext: %s' % msg['msg']
