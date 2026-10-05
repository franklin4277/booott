#property strict
#property version   "1.00"
#property description "Publishes MT5 high-impact calendar events to the local risk engine"

input string InpCalendarFile = "booott_economic_calendar.tsv";
input string InpTemporaryFile = "booott_economic_calendar.tsv.tmp";
input string InpCurrencies = "USD,EUR,GBP,JPY,CHF,CAD,AUD,NZD";
input int    InpLookbackMinutes = 60;
input int    InpLookaheadHours = 48;
input int    InpRefreshSeconds = 30;

bool g_calendar_available = false;

string CleanField(string value)
{
   StringReplace(value, "\t", " ");
   StringReplace(value, "\r", " ");
   StringReplace(value, "\n", " ");
   return value;
}

bool HasEventId(const ulong &event_ids[], const ulong event_id)
{
   for(int i = 0; i < ArraySize(event_ids); i++)
   {
      if(event_ids[i] == event_id)
         return true;
   }
   return false;
}

bool BuildCalendarSnapshot(string &snapshot)
{
   if(InpLookbackMinutes < 0 || InpLookaheadHours < 1 || InpRefreshSeconds < 5)
   {
      Print("Invalid MT5 calendar bridge timing configuration.");
      return false;
   }

   datetime server_now = TimeTradeServer();
   datetime utc_now = TimeGMT();
   if(server_now <= 0 || utc_now <= 0)
   {
      Print("MT5 server or UTC time is unavailable; refusing to publish calendar data.");
      return false;
   }

   datetime server_utc_offset = server_now - utc_now;
   datetime from_time = server_now - InpLookbackMinutes * 60;
   datetime to_time = server_now + InpLookaheadHours * 3600;
   string currencies[];
   int currency_count = StringSplit(InpCurrencies, ',', currencies);
   if(currency_count <= 0)
   {
      Print("No currencies configured for the MT5 economic calendar.");
      return false;
   }

   snapshot = StringFormat("generated_at_utc=%I64d\n", (long)utc_now);
   ulong written_event_ids[];

   for(int currency_index = 0; currency_index < currency_count; currency_index++)
   {
      string currency = currencies[currency_index];
      StringTrimLeft(currency);
      StringTrimRight(currency);
      StringToUpper(currency);
      if(StringLen(currency) == 0)
         continue;

      MqlCalendarValue values[];
      ResetLastError();
      int value_count = CalendarValueHistory(
         values,
         from_time,
         to_time,
         NULL,
         currency
      );
      if(value_count < 0)
      {
         PrintFormat(
            "MT5 calendar query failed for %s (error %d); keeping risk checks fail-closed.",
            currency,
            GetLastError()
         );
         return false;
      }

      for(int value_index = 0; value_index < value_count; value_index++)
      {
         ulong event_id = values[value_index].event_id;
         if(HasEventId(written_event_ids, event_id))
            continue;

         MqlCalendarEvent event;
         if(!CalendarEventById(event_id, event))
         {
            PrintFormat(
               "MT5 calendar event lookup failed for %I64u (error %d).",
               event_id,
               GetLastError()
            );
            return false;
         }
         if(event.importance != CALENDAR_IMPORTANCE_HIGH)
            continue;

         ArrayResize(written_event_ids, ArraySize(written_event_ids) + 1);
         written_event_ids[ArraySize(written_event_ids) - 1] = event_id;

         datetime event_utc = values[value_index].time - server_utc_offset;
         snapshot += StringFormat(
            "%I64u\t%s\tHIGH\t%I64d\t%s\n",
            event_id,
            CleanField(currency),
            (long)event_utc,
            CleanField(event.name)
         );
      }
   }
   return true;
}

void RefreshCalendarSnapshot()
{
   string snapshot;
   if(!BuildCalendarSnapshot(snapshot))
   {
      g_calendar_available = false;
      return;
   }

   ResetLastError();
   int handle = FileOpen(
      InpTemporaryFile,
      FILE_WRITE | FILE_TXT | FILE_UNICODE | FILE_COMMON
   );
   if(handle == INVALID_HANDLE)
   {
      g_calendar_available = false;
      PrintFormat(
         "Cannot write temporary MT5 calendar snapshot %s (error %d).",
         InpTemporaryFile,
         GetLastError()
      );
      return;
   }

   uint written = FileWriteString(handle, snapshot);
   FileFlush(handle);
   FileClose(handle);
   if(written != StringLen(snapshot))
   {
      g_calendar_available = false;
      Print("MT5 calendar snapshot write was incomplete.");
      return;
   }

   bool moved = false;
   if(FileIsExist(InpCalendarFile, FILE_COMMON))
      moved = FileMove(
         InpTemporaryFile,
         FILE_COMMON,
         InpCalendarFile,
         FILE_COMMON | FILE_REWRITE
      );
   else
      moved = FileMove(
         InpTemporaryFile,
         FILE_COMMON,
         InpCalendarFile,
         FILE_COMMON
      );
   if(!moved)
   {
      g_calendar_available = false;
      PrintFormat(
         "Cannot install MT5 calendar snapshot %s (error %d).",
         InpCalendarFile,
         GetLastError()
      );
      return;
   }
   g_calendar_available = true;
}

int OnInit()
{
   if(StringLen(InpCalendarFile) == 0)
      return INIT_PARAMETERS_INCORRECT;
   EventSetTimer(InpRefreshSeconds);
   RefreshCalendarSnapshot();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   RefreshCalendarSnapshot();
}

void OnTick()
{
}
