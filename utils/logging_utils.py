import logging
import traceback
from datetime import datetime

# Create a custom formatter that includes line numbers and optional traceback
class DetailedFormatter(logging.Formatter):
    def format(self, record):
        # Get the relative path to the file instead of full path
        record.pathname = record.pathname.split('/')[-1]
        
        # Standard format with timestamp, level, file:line, and message
        format_str = '%(asctime)s [%(levelname)s] %(pathname)s:%(lineno)d - %(message)s'
        
        # Add exception info for INFO and above
        if record.exc_info and record.levelno >= logging.INFO:
            format_str += '\nException: %(exc_info)s'
        
        # Add full traceback for DEBUG level
        if record.exc_info and record.levelno == logging.DEBUG:
            format_str += '\nTraceback:\n%(exc_text)s'
            if not record.exc_text:
                record.exc_text = ''.join(traceback.format_exception(*record.exc_info))
        
        formatter = logging.Formatter(format_str, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)

# Set up logging configuration
def setup_logging(level=logging.INFO):
    # Create logger
    logger = logging.getLogger(__name__)
    logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicate logs
    logger.handlers = []
    
    # Create console handler with custom formatter
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(DetailedFormatter())
    
    # Add handler to logger
    logger.addHandler(console_handler)
    
    return logger