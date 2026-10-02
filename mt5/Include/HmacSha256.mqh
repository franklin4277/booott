#ifndef HMAC_SHA256_MQH
#define HMAC_SHA256_MQH

uint HmacRotr(const uint value, const int bits)
{
   return (value >> bits) | (value << (32 - bits));
}

uint HmacCh(const uint x, const uint y, const uint z)
{
   return (x & y) ^ (~x & z);
}

uint HmacMaj(const uint x, const uint y, const uint z)
{
   return (x & y) ^ (x & z) ^ (y & z);
}

void Sha256Hash(const uchar &data[], const int length, uchar &digest[])
{
   uint k[64] = {
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
   };

   int padded = length + 9;
   int extra = (64 - (padded % 64)) % 64;
   int total = padded + extra;
   uchar block[];
   ArrayResize(block, total);
   ArrayInitialize(block, 0);
   for(int i = 0; i < length; i++)
      block[i] = data[i];
   block[length] = 0x80;
   ulong bit_len = (ulong)length * 8;
   for(int i = 0; i < 8; i++)
      block[total - 1 - i] = (uchar)((bit_len >> (8 * i)) & 0xff);

   uint h0 = 0x6a09e667;
   uint h1 = 0xbb67ae85;
   uint h2 = 0x3c6ef372;
   uint h3 = 0xa54ff53a;
   uint h4 = 0x510e527f;
   uint h5 = 0x9b05688c;
   uint h6 = 0x1f83d9ab;
   uint h7 = 0x5be0cd19;

   uint w[64];
   for(int offset = 0; offset < total; offset += 64)
   {
      for(int i = 0; i < 16; i++)
      {
         int p = offset + i * 4;
         w[i] = ((uint)block[p] << 24) | ((uint)block[p + 1] << 16) |
                ((uint)block[p + 2] << 8) | (uint)block[p + 3];
      }
      for(int i = 16; i < 64; i++)
      {
         uint s0 = HmacRotr(w[i - 15], 7) ^ HmacRotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
         uint s1 = HmacRotr(w[i - 2], 17) ^ HmacRotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
         w[i] = w[i - 16] + s0 + w[i - 7] + s1;
      }

      uint a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, hh = h7;
      for(int i = 0; i < 64; i++)
      {
         uint s1 = HmacRotr(e, 6) ^ HmacRotr(e, 11) ^ HmacRotr(e, 25);
         uint t1 = hh + s1 + HmacCh(e, f, g) + k[i] + w[i];
         uint s0 = HmacRotr(a, 2) ^ HmacRotr(a, 13) ^ HmacRotr(a, 22);
         uint t2 = s0 + HmacMaj(a, b, c);
         hh = g;
         g = f;
         f = e;
         e = d + t1;
         d = c;
         c = b;
         b = a;
         a = t1 + t2;
      }
      h0 += a; h1 += b; h2 += c; h3 += d; h4 += e; h5 += f; h6 += g; h7 += hh;
   }

   ArrayResize(digest, 32);
   uint hs[8] = {h0, h1, h2, h3, h4, h5, h6, h7};
   for(int i = 0; i < 8; i++)
   {
      digest[i * 4] = (uchar)((hs[i] >> 24) & 0xff);
      digest[i * 4 + 1] = (uchar)((hs[i] >> 16) & 0xff);
      digest[i * 4 + 2] = (uchar)((hs[i] >> 8) & 0xff);
      digest[i * 4 + 3] = (uchar)(hs[i] & 0xff);
   }
}

string BytesToHex(const uchar &bytes[])
{
   string hex = "";
   int size = ArraySize(bytes);
   for(int i = 0; i < size; i++)
      hex += StringFormat("%02x", bytes[i]);
   return hex;
}

string HmacSha256Hex(const string key, const string message)
{
   uchar key_bytes[];
   uchar msg_bytes[];
   StringToCharArray(key, key_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   StringToCharArray(message, msg_bytes, 0, WHOLE_ARRAY, CP_UTF8);
   int key_len = ArraySize(key_bytes);
   int msg_len = ArraySize(msg_bytes);
   if(key_len > 0 && key_bytes[key_len - 1] == 0)
      key_len--;
   if(msg_len > 0 && msg_bytes[msg_len - 1] == 0)
      msg_len--;

   uchar key_block[64];
   ArrayInitialize(key_block, 0);
   if(key_len > 64)
   {
      uchar hashed_key[];
      Sha256Hash(key_bytes, key_len, hashed_key);
      for(int i = 0; i < 32; i++)
         key_block[i] = hashed_key[i];
   }
   else
   {
      for(int i = 0; i < key_len; i++)
         key_block[i] = key_bytes[i];
   }

   uchar ipad[64];
   uchar opad[64];
   for(int i = 0; i < 64; i++)
   {
      ipad[i] = (uchar)(key_block[i] ^ 0x36);
      opad[i] = (uchar)(key_block[i] ^ 0x5c);
   }

   uchar inner[];
   ArrayResize(inner, 64 + msg_len);
   for(int i = 0; i < 64; i++)
      inner[i] = ipad[i];
   for(int i = 0; i < msg_len; i++)
      inner[64 + i] = msg_bytes[i];

   uchar inner_hash[];
   Sha256Hash(inner, 64 + msg_len, inner_hash);

   uchar outer[];
   ArrayResize(outer, 96);
   for(int i = 0; i < 64; i++)
      outer[i] = opad[i];
   for(int i = 0; i < 32; i++)
      outer[64 + i] = inner_hash[i];

   uchar digest[];
   Sha256Hash(outer, 96, digest);
   return BytesToHex(digest);
}

bool HmacEquals(const string left, const string right)
{
   if(StringLen(left) != StringLen(right))
      return false;
   int diff = 0;
   int length = StringLen(left);
   for(int i = 0; i < length; i++)
      diff |= StringGetCharacter(left, i) ^ StringGetCharacter(right, i);
   return diff == 0;
}

#endif
