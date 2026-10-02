#ifndef JSON_LITE_MQH
#define JSON_LITE_MQH

string JsonExtractRaw(const string json, const string key)
{
   string token = "\"" + key + "\"";
   int key_pos = StringFind(json, token);
   if(key_pos < 0)
      return "";
   int colon = StringFind(json, ":", key_pos + StringLen(token));
   if(colon < 0)
      return "";
   int start = colon + 1;
   int length = StringLen(json);
   while(start < length)
   {
      ushort c = StringGetCharacter(json, start);
      if(c != ' ' && c != '\t' && c != '\r' && c != '\n')
         break;
      start++;
   }
   if(start >= length)
      return "";
   if(StringSubstr(json, start, 4) == "null")
      return "";
   if(StringGetCharacter(json, start) == '"')
   {
      int end = start + 1;
      while(end < length)
      {
         ushort c = StringGetCharacter(json, end);
         if(c == '\\')
         {
            end += 2;
            continue;
         }
         if(c == '"')
            break;
         end++;
      }
      return StringSubstr(json, start + 1, end - start - 1);
   }
   int end = start;
   while(end < length)
   {
      ushort c = StringGetCharacter(json, end);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\t' || c == '\r' || c == '\n')
         break;
      end++;
   }
   return StringSubstr(json, start, end - start);
}

string JsonCanonicalHmacMessage(const string json)
{
   string keys[13] = {
      "order_id", "intent_id", "symbol", "side", "order_type", "volume",
      "price", "stop_loss", "take_profit", "issued_at", "expires_at",
      "nonce", "trace_id"
   };
   string message = "";
   for(int i = 0; i < 13; i++)
   {
      if(i > 0)
         message += "|";
      message += JsonExtractRaw(json, keys[i]);
   }
   return message;
}

#endif
