#property strict
#property version   "1.10"
#property description "Standalone account equity and daily drawdown emergency guard"

#include <Trade/Trade.mqh>

input double InpMaxDailyLossPercent = 5.0;
input double InpHardEquityFloor = 500.0;
input string InpConfigFile = "equity_guard_config.json";
input string InpCrashLogFile = "equity_guard_crash.log";
input string InpWatchdogHeartbeatFile = "equity_guard_heartbeat.dat";
input string InpTradingLockFile = "trading_state.lock";
input int    InpHeartbeatSeconds = 1;

CTrade trade;

double g_equity_floor = 0.0;
double g_max_daily_drawdown_pct = 0.0;
double g_day_start_equity = 0.0;
int    g_server_date = 0;
bool   g_triggered = false;
bool   g_alarm_played = false;
bool   g_trading_disabled_warning_logged = false;
bool   g_completion_logged = false;

void WriteWatchdogHeartbeat()
{
   int handle = FileOpen(InpWatchdogHeartbeatFile,
                         FILE_WRITE | FILE_TXT | FILE_ANSI |
                         FILE_COMMON | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("WARNING: Cannot write watchdog heartbeat %s (error %d).",
                  InpWatchdogHeartbeatFile, GetLastError());
      return;
   }

   FileWriteString(handle, IntegerToString((int)TimeGMT()));
   FileFlush(handle);
   FileClose(handle);
}

void WriteTradingLock(const string state)
{
   int handle = FileOpen(InpTradingLockFile,
                         FILE_WRITE | FILE_TXT | FILE_ANSI |
                         FILE_COMMON | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("CRITICAL: Cannot write trading lock %s (error %d).",
                  InpTradingLockFile, GetLastError());
      return;
   }
   FileWriteString(handle, state);
   FileFlush(handle);
   FileClose(handle);
}

bool ReadNumber(const string json, const string key, double &value)
{
   string token = "\"" + key + "\"";
   int key_pos = StringFind(json, token);
   if(key_pos < 0)
      return false;

   int colon_pos = StringFind(json, ":", key_pos + StringLen(token));
   if(colon_pos < 0)
      return false;

   int start = colon_pos + 1;
   int length = StringLen(json);
   while(start < length)
   {
      ushort c = StringGetCharacter(json, start);
      if(c != ' ' && c != '\t' && c != '\r' && c != '\n')
         break;
      start++;
   }

   int end = start;
   bool has_digit = false;
   while(end < length)
   {
      ushort c = StringGetCharacter(json, end);
      if(c == ',' || c == '}' || c == ' ' || c == '\t' || c == '\r' || c == '\n')
         break;
      if(c >= '0' && c <= '9')
         has_digit = true;
      else if(c != '+' && c != '-' && c != '.' && c != 'e' && c != 'E')
         return false;
      end++;
   }

   if(end <= start || !has_digit)
      return false;

   string number_text = StringSubstr(json, start, end - start);
   value = StringToDouble(number_text);
   return MathIsValidNumber(value);
}

bool LoadConfiguration()
{
   g_equity_floor = InpHardEquityFloor;
   g_max_daily_drawdown_pct = InpMaxDailyLossPercent;

   int handle = FileOpen(InpConfigFile, FILE_READ | FILE_BIN | FILE_SHARE_READ);
   if(handle != INVALID_HANDLE)
   {
      string json = FileReadString(handle, (int)FileSize(handle));
      FileClose(handle);
      double floor_from_file = 0.0;
      double dd_from_file = 0.0;
      if(ReadNumber(json, "absolute_equity_floor", floor_from_file))
         g_equity_floor = floor_from_file;
      if(ReadNumber(json, "max_daily_drawdown_percent", dd_from_file) ||
         ReadNumber(json, "MaxDailyLossPercent", dd_from_file) ||
         ReadNumber(json, "HardEquityFloor", floor_from_file))
      {
         if(ReadNumber(json, "max_daily_drawdown_percent", dd_from_file) ||
            ReadNumber(json, "MaxDailyLossPercent", dd_from_file))
            g_max_daily_drawdown_pct = dd_from_file;
         if(ReadNumber(json, "HardEquityFloor", floor_from_file) ||
            ReadNumber(json, "absolute_equity_floor", floor_from_file))
            g_equity_floor = floor_from_file;
      }
   }

   if(g_equity_floor <= 0.0 ||
      g_max_daily_drawdown_pct <= 0.0 ||
      g_max_daily_drawdown_pct > 100.0)
   {
      Print("ERROR: HardEquityFloor must be positive and MaxDailyLossPercent must be in (0, 100].");
      return false;
   }

   return true;
}

int CurrentServerDate()
{
   MqlDateTime now;
   TimeToStruct(TimeCurrent(), now);
   return now.year * 10000 + now.mon * 100 + now.day;
}

string DailyEquityVariableName(const int server_date)
{
   return StringFormat("EqGuard.%I64d.%d", AccountInfoInteger(ACCOUNT_LOGIN), server_date);
}

void InitializeDailyBaseline()
{
   g_server_date = CurrentServerDate();
   string variable_name = DailyEquityVariableName(g_server_date);

   if(GlobalVariableCheck(variable_name))
      g_day_start_equity = GlobalVariableGet(variable_name);
   else
   {
      g_day_start_equity = AccountInfoDouble(ACCOUNT_EQUITY);
      GlobalVariableSet(variable_name, g_day_start_equity);
   }

   PrintFormat("Daily equity baseline: %.2f; HardEquityFloor: %.2f; MaxDailyLossPercent: %.2f%%.",
               g_day_start_equity, g_equity_floor, g_max_daily_drawdown_pct);
}

void RefreshDailyBaselineIfNeeded()
{
   int today = CurrentServerDate();
   if(today == g_server_date)
      return;

   InitializeDailyBaseline();
}

void AppendCrashLog(const string message)
{
   int handle = FileOpen(InpCrashLogFile,
                         FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
   {
      handle = FileOpen(InpCrashLogFile,
                        FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   }

   if(handle == INVALID_HANDLE)
   {
      PrintFormat("CRITICAL: Cannot write %s (error %d). %s",
                  InpCrashLogFile, GetLastError(), message);
      return;
   }

   FileSeek(handle, 0, SEEK_END);
   FileWrite(handle,
             TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS),
             " [CRITICAL] ",
             message);
   FileFlush(handle);
   FileClose(handle);
}

void DisableAlgorithmicTrading()
{
   WriteTradingLock("EMERGENCY_FLAT");
   if(!(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !(bool)MQLInfoInteger(MQL_TRADE_ALLOWED) ||
      !(bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
   {
      AppendCrashLog("Algorithmic trading already disabled by terminal, EA, or account settings.");
      return;
   }
   AppendCrashLog(
      "EMERGENCY_FLAT lock written. Disable the terminal AutoTrading button immediately; MQL5 has no official API to toggle it.");
}

void TriggerGuard(const string reason, const double equity)
{
   if(!g_triggered)
   {
      g_triggered = true;
      string message = StringFormat(
         "EQUITY GUARD TRIGGERED: %s Current equity: %.2f. All positions will be flattened and new algorithmic orders are locked.",
         reason, equity);
      AppendCrashLog(message);
      Print(message);
      Alert(message);
      DisableAlgorithmicTrading();

      if(!g_alarm_played)
      {
         PlaySound("alert.wav");
         g_alarm_played = true;
      }
   }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) &&
      !g_trading_disabled_warning_logged)
   {
      AppendCrashLog("Trading is not allowed by terminal/account settings; liquidation attempts may be rejected.");
      g_trading_disabled_warning_logged = true;
   }
}

bool LiquidatePositions()
{
   bool all_closed = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
      {
         all_closed = false;
         AppendCrashLog(StringFormat("Could not select position at index %d (error %d).",
                                     i, GetLastError()));
         continue;
      }

      bool request_ok = trade.PositionClose(ticket);
      uint retcode = trade.ResultRetcode();
      if(!request_ok || retcode != TRADE_RETCODE_DONE)
      {
         all_closed = false;
         AppendCrashLog(StringFormat(
            "Failed to close position ticket %I64u: %s (retcode %u).",
            ticket, trade.ResultRetcodeDescription(), retcode));
      }
      else
      {
         PrintFormat("Closed position ticket %I64u.", ticket);
      }
   }

   return all_closed;
}

bool CancelPendingOrders()
{
   bool all_deleted = true;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
      {
         all_deleted = false;
         AppendCrashLog(StringFormat("Could not select order at index %d (error %d).",
                                     i, GetLastError()));
         continue;
      }

      bool request_ok = trade.OrderDelete(ticket);
      uint retcode = trade.ResultRetcode();
      if(!request_ok || retcode != TRADE_RETCODE_DONE)
      {
         all_deleted = false;
         AppendCrashLog(StringFormat(
            "Failed to cancel pending order ticket %I64u: %s (retcode %u).",
            ticket, trade.ResultRetcodeDescription(), retcode));
      }
      else
      {
         PrintFormat("Cancelled pending order ticket %I64u.", ticket);
      }
   }

   return all_deleted;
}

void RunLiquidation()
{
   bool positions_closed = LiquidatePositions();
   bool orders_cancelled = CancelPendingOrders();

   if(positions_closed && orders_cancelled &&
      PositionsTotal() == 0 && OrdersTotal() == 0 && !g_completion_logged)
   {
      AppendCrashLog("Liquidation pass complete: no open positions or pending orders remain.");
      g_completion_logged = true;
   }
}

void CheckEquity()
{
   if(g_triggered)
   {
      RunLiquidation();
      return;
   }

   RefreshDailyBaselineIfNeeded();

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double daily_floor = g_day_start_equity *
                        (1.0 - g_max_daily_drawdown_pct / 100.0);

   if(equity < g_equity_floor)
      TriggerGuard(StringFormat("equity %.2f fell below HardEquityFloor %.2f",
                                equity, g_equity_floor), equity);
   else if(equity < daily_floor)
      TriggerGuard(StringFormat("equity %.2f fell below MaxDailyLossPercent threshold %.2f",
                                equity, daily_floor), equity);

   if(g_triggered)
      RunLiquidation();
}

int OnInit()
{
   if(!LoadConfiguration())
   {
      Alert("IndependentEquityGuard failed to load a valid local configuration; guard is not active.");
      return INIT_FAILED;
   }

   trade.SetAsyncMode(false);
   trade.SetExpertMagicNumber(0);
   InitializeDailyBaseline();

   int timer_seconds = InpHeartbeatSeconds;
   if(timer_seconds < 1)
      timer_seconds = 1;
   EventSetTimer(timer_seconds);
   WriteWatchdogHeartbeat();

   Print("IndependentEquityGuard active. Attach it to a dedicated clean chart.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick()
{
   CheckEquity();
}

void OnTimer()
{
   WriteWatchdogHeartbeat();
   CheckEquity();
}
