READY_STATE = "ready"
CONNECTING_STATE = "connecting"
ERROR_STATE = "error"
DOWNLOADING_STATE = "downloading"
PRINTING_STATE = "printing"
PAUSED_STATE = "paused"
CANCEL_STATE = "cancel"
LOCAL_STATE = "local_mode"
BED_CLEAN_STATE = "bed_not_clear"
# CLOSING_STATE = "closing"

ALL_STATES = (
              READY_STATE,
              CONNECTING_STATE,
              ERROR_STATE,
              DOWNLOADING_STATE,
              PRINTING_STATE,
              PAUSED_STATE,
              CANCEL_STATE,
              LOCAL_STATE,
              BED_CLEAN_STATE
              )

CANCEL_THREASHOLD = 99 

def process_state_change(prev_report, next_report):
    if prev_report and next_report:
        if prev_report['state'] == PRINTING_STATE and next_report['state'] != PRINTING_STATE:
            prev_progress = prev_report.get('percent', 0)
            next_progress = next_report.get('percent', 0)
            if prev_progress != 100 and prev_progress > 0 and next_progress < CANCEL_THREASHOLD:
                return [{'state:', 'cancel'}]
