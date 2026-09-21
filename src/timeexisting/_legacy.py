#!/usr/bin/env python3
"""
TimeExisting: The Existential Progress Tracker - OPTIMIZED
A dark humor-themed script to track various time metrics
with philosophical commentary on the futility of existence

Now with pure Rich components, smooth Live updates, and showcase mode!
"""

import time
import random
import math
from datetime import datetime, timedelta
import os
import sys
import argparse
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn
from rich.text import Text
from rich.table import Table
from rich.align import Align
from rich import box
from rich.live import Live
from rich.layout import Layout
from rich.columns import Columns

def load_ascii_art(filename):
    """Load ASCII art from file"""
    try:
        # Get the directory where the script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filepath = os.path.join(script_dir, 'ascii_art', filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read().rstrip()  # rstrip() removes trailing newlines
    except:
        return ""

# Initialize Rich console
console = Console()

# ASCII Art loading
ASCII_ART = load_ascii_art("header.txt")
COFFEE_ART = load_ascii_art("coffee.txt")
SUNSET_ART = load_ascii_art("sunset.txt")
LUNCH_ART = load_ascii_art("lunch.txt")
BREAK_ART_MORNING = load_ascii_art("break_morning.txt")
BREAK_ART_AFTERNOON = load_ascii_art("break_afternoon.txt")
SHUTDOWN_ART = load_ascii_art("shutdown.txt")
GAMING_ASCII = load_ascii_art("gaming.txt")
INTIMATE_ASCII = load_ascii_art("intimate.txt")
READING_ASCII = load_ascii_art("reading.txt")
SLEEP_ASCII = load_ascii_art("sleep.txt")
PRESLEEP_ASCII = load_ascii_art("presleep.txt")
BOULDERING_ASCII = load_ascii_art("surf.txt")
MOUNTAIN_BIKE_ASCII = load_ascii_art("mtb.txt")
HIKING_ASCII = load_ascii_art("fencing.txt")
CODING_ASCII = load_ascii_art("chess.txt")

# Configuration
REFRESH_INTERVAL = 1  # Update every second for smooth time display
START_HOUR = 9
END_HOUR = 18
WARNING_START_HOUR = 17
WARNING_START_MINUTE = 45

# Sleep configuration
SLEEP_HOUR = 23  # 11pm
WAKE_HOUR = 7    # 7am

# Break times
MORNING_BREAK_START = (11, 0)  # 11:00
MORNING_BREAK_END = (11, 15)   # 11:15
LUNCH_BREAK_START = (13, 0)    # 13:00
LUNCH_BREAK_END = (14, 0)      # 14:00
AFTERNOON_BREAK_START = (16, 0) # 16:00
AFTERNOON_BREAK_END = (16, 15)  # 16:15

# Month remarks - philosophical observations about each month
MONTH_REMARKS = {
    1: "January: False hope of new beginnings in the dead of winter. The year stretches before you like an endless void.",
    2: "February: Time compresses in this truncated month, yet the weight of existence remains unbearably constant.",
    3: "March: Nature awakens while your spirit remains dormant, frozen in the permafrost of routine.",
    4: "April: Rain washes away winter's remnants, but nothing can cleanse the stains of past regrets.",
    5: "May: The blossoms of possibility bloom briefly before withering under reality's harsh glare.",
    6: "June: Solstice approaches—the longest day only emphasizes how short life truly is.",
    7: "July: The inferno of summer mirrors the burning of time slipping through your fingers.",
    8: "August: Summer's death rattle echoes across sun-scorched days as autumn's shadow looms.",
    9: "September: The year begins its inexorable decline, leaves falling like abandoned dreams.",
    10: "October: As nature dons its death shroud, we confront our own impermanence.",
    11: "November: Barren trees stand like memento mori against gray skies, reminding us of what we've lost.",
    12: "December: The year gasps its final breaths as we delude ourselves that the next will somehow be different."
}

# Season definitions and remarks
SEASONS = {
    "Winter": [12, 1, 2],
    "Spring": [3, 4, 5],
    "Summer": [6, 7, 8],
    "Fall": [9, 10, 11]
}

SEASON_REMARKS = {
    "Winter": "Winter: The barren landscape mirrors the frozen ambitions within your soul as time crystallizes into meaningless iterations.",
    "Spring": "Spring: Nature's cruel joke of renewal, giving false hope while your tasks multiply like weeds in corporate soil.",
    "Summer": "Summer: The sun's oppressive glare exposes the futility of your efforts as others pretend to enjoy their fleeting vacation escapes.",
    "Fall": "Fall: As leaves wither and die, so too do your dreams of advancement and purpose in this corporate wasteland."
}

# Weekday comments
WEEKDAY_COMMENTS = {
    0: "Sunday: The dread of tomorrow poisons today's fleeting freedom. Savor these final moments of self-delusion.",
    1: "Monday: As the first chains of the week shackle you to your desk, remember: only 96 hours of corporate servitude remain.",
    2: "Tuesday: The illusion of progress sets in as you trudge through the wasteland of meetings and deadlines.",
    3: "Wednesday: Balanced on the knife-edge of the week, neither here nor there—just like your career aspirations.",
    4: "Thursday: So close to freedom, yet the hours stretch like an existential void. The weekend's siren song grows louder.",
    5: "Friday: As the corporate prison prepares for weekend lockdown, enjoy your brief taste of impending freedom.",
    6: "Saturday: Temporary liberation that passes all too quickly. The clock is already ticking toward Monday's inevitable return."
}

# Weekend activity messages
WEEKEND_ACTIVITIES = [
    ("gaming", "🎮 Escaping reality through digital worlds, because this one disappointed you...", GAMING_ASCII),
    ("intimate", "💕 Seeking fleeting connection in a disconnected world...", INTIMATE_ASCII),
    ("reading", "📚 Filling your mind with others' thoughts to avoid confronting your own...", READING_ASCII),
    ("sleep", "😴 Unconsciousness: the only true escape from existential dread...", SLEEP_ASCII),
    ("bouldering", "🧗 Climbing fake rocks to feel something real in your cushioned existence...", BOULDERING_ASCII),
    ("biking", "🚵 Pedaling furiously to nowhere, a perfect metaphor for life...", MOUNTAIN_BIKE_ASCII),
    ("hiking", "🥾 Walking in circles on marked trails, pretending it's an adventure...", HIKING_ASCII),
    ("coding", "💻 Writing code on weekends because capitalism colonized your hobbies too...", CODING_ASCII),
]

# Season colors (using Rich color names)
SEASON_COLORS = {
    "Winter": "blue",
    "Spring": "green",
    "Summer": "yellow",
    "Fall": "dark_orange3"
}

# Weekday colors
WEEKDAY_COLORS = {
    0: "blue",       # Sunday
    1: "red",        # Monday
    2: "yellow3",    # Tuesday
    3: "yellow",     # Wednesday
    4: "cyan",       # Thursday
    5: "green",      # Friday
    6: "magenta",    # Saturday
}

# Existential messages that appear during work hours
EXISTENTIAL_MESSAGES = [
    "The void stares back as you click away your existence...",
    "Each keystroke brings you microscopically closer to freedom...",
    "Time moves slower within the corporate dimension...",
    "The pixels on your screen slowly consume your soul...",
    "Another day in the capitalist machinery...",
    "Your productivity is inversely proportional to your will to live...",
    "Somewhere, somehow, a spreadsheet is being updated...",
    "The fluorescent lights flicker in sympathy with your spirit...",
    "Your dreams wait patiently outside, growing ever dimmer...",
    "Coffee: the elixir that transforms despair into productivity..."
]

def get_current_season(month):
    """Determine the current season based on the month"""
    for season, months in SEASONS.items():
        if month in months:
            return season
    return "Unknown"

def is_break_time(now):
    """Check if current time is during a break period"""
    current_time = (now.hour, now.minute)
    
    # Check morning break
    if (MORNING_BREAK_START[0] == current_time[0] and MORNING_BREAK_START[1] <= current_time[1]) and \
       (MORNING_BREAK_END[0] == current_time[0] and current_time[1] < MORNING_BREAK_END[1]):
        return "morning", MORNING_BREAK_END[1] - current_time[1]
    
    # Check lunch break
    if (LUNCH_BREAK_START[0] <= current_time[0] < LUNCH_BREAK_END[0]) or \
       (LUNCH_BREAK_START[0] == current_time[0] and current_time[1] >= LUNCH_BREAK_START[1]) or \
       (LUNCH_BREAK_END[0] == current_time[0] and current_time[1] < LUNCH_BREAK_END[1]):
        if current_time[0] == LUNCH_BREAK_END[0]:
            minutes_left = LUNCH_BREAK_END[1] - current_time[1]
        else:
            minutes_left = (LUNCH_BREAK_END[0] - current_time[0] - 1) * 60 + (60 - current_time[1]) + LUNCH_BREAK_END[1]
        return "lunch", minutes_left
    
    # Check afternoon break
    if (AFTERNOON_BREAK_START[0] == current_time[0] and AFTERNOON_BREAK_START[1] <= current_time[1]) and \
       (AFTERNOON_BREAK_END[0] == current_time[0] and current_time[1] < AFTERNOON_BREAK_END[1]):
        return "afternoon", AFTERNOON_BREAK_END[1] - current_time[1]
    
    return None, 0

class ShowcaseState:
    """Manages showcase mode state"""
    def __init__(self):
        self.start_time = datetime.now()
        self.cycle_duration = 5  # seconds per state
        self.current_month = 1
        self.current_weekday = 0
        self.current_hour = 6
        self.current_activity = 0
        self.break_cycle = 0  # 0: morning, 1: lunch, 2: afternoon
        
    def get_simulated_time(self):
        """Generate a simulated time for showcase mode"""
        elapsed = (datetime.now() - self.start_time).total_seconds()
        cycle = int(elapsed / self.cycle_duration)
        
        # Cycle through different scenarios
        scenario = cycle % 10  # 10 different scenarios
        
        if scenario == 0:  # Pre-work
            return datetime.now().replace(hour=7, minute=30, second=0, month=self.current_month)
        elif scenario == 1:  # Morning work
            return datetime.now().replace(hour=10, minute=15, second=0, month=self.current_month)
        elif scenario == 2:  # Morning break
            return datetime.now().replace(hour=11, minute=5, second=0, month=self.current_month)
        elif scenario == 3:  # Lunch break
            return datetime.now().replace(hour=13, minute=30, second=0, month=self.current_month)
        elif scenario == 4:  # Afternoon work
            return datetime.now().replace(hour=15, minute=0, second=0, month=self.current_month)
        elif scenario == 5:  # Afternoon break
            return datetime.now().replace(hour=16, minute=5, second=0, month=self.current_month)
        elif scenario == 6:  # End of work
            return datetime.now().replace(hour=18, minute=30, second=0, month=self.current_month)
        elif scenario == 7:  # Weekend Saturday
            self.current_weekday = 5
            return datetime.now().replace(hour=14, minute=0, second=0, month=self.current_month) + timedelta(days=5-datetime.now().weekday())
        elif scenario == 8:  # Weekend Sunday
            self.current_weekday = 6
            return datetime.now().replace(hour=16, minute=0, second=0, month=self.current_month) + timedelta(days=6-datetime.now().weekday())
        else:  # Cycle months
            self.current_month = (self.current_month % 12) + 1
            return datetime.now().replace(hour=12, minute=0, second=0, month=self.current_month)
            
    def get_current_activity(self):
        """Get current weekend activity for showcase"""
        self.current_activity = (self.current_activity + 1) % len(WEEKEND_ACTIVITIES)
        return WEEKEND_ACTIVITIES[self.current_activity]

class ExistentialTracker:
    """Main class to handle all the existential tracking with proper state management"""
    
    def __init__(self, showcase_mode=False):
        self.showcase_mode = showcase_mode
        self.showcase_state = ShowcaseState() if showcase_mode else None
        self.last_activity_time = time.time()
        self.current_weekend_activity = None
        
    def get_current_time(self):
        """Get current time - real or simulated for showcase"""
        if self.showcase_mode:
            return self.showcase_state.get_simulated_time()
        return datetime.now()
            
    def make_header(self):
        """Create the header panel with ASCII art"""
        if ASCII_ART:
            header_text = Text(ASCII_ART, style="yellow", justify="center")
            subtitle = Text("The Existential Progress Tracker: Because Tracking Your Slow Descent Into Madness Is Important", 
                          style="bold yellow", justify="center")
            content = Table.grid()
            content.add_row(header_text)
            content.add_row("")  # Add spacing
            content.add_row(subtitle)
            return Panel(content, box=box.DOUBLE, border_style="yellow", expand=True)
        else:
            header_text = Text("The Existential Progress Tracker: Because Tracking Your Slow Descent Into Madness Is Important", 
                              style="bold yellow", justify="center")
            return Panel(header_text, box=box.DOUBLE, border_style="yellow")
    
    def make_year_panel(self, now):
        """Create year progress panel"""
        start_of_year = datetime(now.year, 1, 1)
        end_of_year = datetime(now.year, 12, 31)
        total_days = (end_of_year - start_of_year).days + 1
        day_of_year = (now - start_of_year).days + 1
        
        month_number = now.month
        month_name = now.strftime("%B")
        days_in_month = (datetime(now.year, month_number % 12 + 1, 1) if month_number < 12 
                        else datetime(now.year + 1, 1, 1)) - datetime(now.year, month_number, 1)
        days_in_month = days_in_month.days
        day_of_month = now.day
        
        current_season = get_current_season(month_number)
        season_color = SEASON_COLORS[current_season]
        
        year_table = Table(box=box.ROUNDED, show_header=False, expand=True)
        year_table.add_column("Info", style="dim")
        year_table.add_column("Value")
        
        # Year progress
        year_percent = round((day_of_year / total_days) * 100, 1)
        year_table.add_row("Year Progress", f"{year_percent}%")
        year_table.add_row("", f"[progress.percentage]{day_of_year}/{total_days} days[/progress.percentage]")
        
        # Month progress
        month_percent = round((day_of_month / days_in_month) * 100, 1)
        year_table.add_row("Month", f"{month_name} - {month_percent}%")
        year_table.add_row("", f"[progress.percentage]{day_of_month}/{days_in_month} days[/progress.percentage]")
        
        # Season info
        year_table.add_row("Season", f"[{season_color}]{current_season}[/{season_color}]")
        year_table.add_row("Season Remark", Text(SEASON_REMARKS[current_season], style="italic"))
        year_table.add_row("Month Remark", Text(MONTH_REMARKS[month_number], style="italic"))
        
        days_left = total_days - day_of_year
        footer = Text(f"Only {days_left} days left to endure this year...", style="dim red", justify="center")
        
        return Panel(
            year_table,
            title="ANNUAL EXISTENTIAL METER",
            border_style=season_color,
            expand=True,
            subtitle=footer
        )
    
    def make_week_panel(self, now):
        """Create week progress panel"""
        weekday = now.weekday()
        weekday_name = now.strftime("%A")
        day_of_week = (weekday + 1) % 7
        
        completed_days = min(weekday, 5) if weekday < 5 else 5
        total_work_days = 5
        percent_complete = round((completed_days / total_work_days) * 100, 1)
        
        comment = WEEKDAY_COMMENTS[day_of_week]
        day_color = WEEKDAY_COLORS[day_of_week]
        
        week_table = Table(box=box.ROUNDED, show_header=False, expand=True)
        week_table.add_column("Info", style="dim")
        week_table.add_column("Value")
        
        if day_of_week >= 1 and day_of_week <= 5:
            week_table.add_row(f"{weekday_name}", f"{percent_complete}% of the workweek complete")
        else:
            week_table.add_row(f"{weekday_name}", "Weekend: A brief respite in the void")
        
        week_table.add_row("Comment", Text(comment, style="italic"))
        
        footer = ""
        if day_of_week >= 1 and day_of_week <= 4:
            days_left = 5 - day_of_week
            footer = f"Only {days_left} more days until temporary weekend freedom..."
        elif day_of_week == 0:
            footer = "The abyss of Monday looms just hours away..."
        elif day_of_week == 6:
            footer = "Sunday's existential dread approaches..."
        
        footer_text = Text(footer, style="dim yellow", justify="center") if footer else None
        
        return Panel(
            week_table,
            title="WEEKLY SOUL DRAIN",
            border_style=day_color,
            expand=True,
            subtitle=footer_text
        )
    
    def make_weekend_panel(self, now):
        """Create weekend vibes panel"""
        # Rotate activities every 30 seconds or in showcase mode
        if self.showcase_mode or (time.time() - self.last_activity_time > 30):
            if self.showcase_mode:
                self.current_weekend_activity = self.showcase_state.get_current_activity()
            else:
                self.current_weekend_activity = random.choice(WEEKEND_ACTIVITIES)
            self.last_activity_time = time.time()
        
        if not self.current_weekend_activity:
            self.current_weekend_activity = random.choice(WEEKEND_ACTIVITIES)
        
        activity_name, message, ascii_art = self.current_weekend_activity
        
        content = Table.grid(padding=1)
        content.add_row(Text("🌈 WEEKEND VIBES", style="bold magenta", justify="center"))
        content.add_row(Text(message, style="italic cyan", justify="center"))
        
        if ascii_art and self.showcase_mode:  # Only show ASCII in showcase mode
            # Add ASCII art as a single text block to preserve formatting
            content.add_row(Text(ascii_art, style="dim"))
        
        content.add_row(Text("Time is a construct. Waste it wisely.", style="dim yellow", justify="center"))
        content.add_row(Text(f"Current time: {now.strftime('%H:%M:%S')}", style="cyan", justify="center"))
        
        return Panel(content, title="EXISTENTIAL WEEKEND MODE", border_style="magenta", expand=True)
    
    def make_break_panel(self, break_type, minutes_left, now):
        """Create break time panel with ASCII art"""
        break_configs = {
            "morning": {
                "emoji": "☕",
                "message": "Quick! Pretend to enjoy this mandatory wellness break...",
                "ascii": BREAK_ART_MORNING,
                "color": "yellow"
            },
            "lunch": {
                "emoji": "🍽️",
                "message": "One hour to consume sustenance and pretend you have a life outside these walls...",
                "ascii": LUNCH_ART,
                "color": "green"
            },
            "afternoon": {
                "emoji": "☕",
                "message": "The afternoon slump demands its caffeinated sacrifice...",
                "ascii": BREAK_ART_AFTERNOON,
                "color": "cyan"
            }
        }
        
        config = break_configs[break_type]
        
        content = Table.grid(padding=1)
        content.add_row(Text(f"{config['emoji']} {break_type.upper()} BREAK", style=f"bold {config['color']}", justify="center"))
        content.add_row(Text(config['message'], style="italic", justify="center"))
        
        if config['ascii'] and self.showcase_mode:
            # Add ASCII art as a single text block
            content.add_row(Text(config['ascii'], style="dim"))
        
        content.add_row(Text(f"Time remaining: {minutes_left} minutes", style="yellow", justify="center"))
        content.add_row(Text(f"Current time: {now.strftime('%H:%M:%S')}", style="cyan", justify="center"))
        
        # Let the panel size itself based on content
        return Panel(content, title="BREAK TIME", border_style=config['color'])
    
    def make_time_panel(self, now):
        """Create the dynamic time panel"""
        start_time = datetime(now.year, now.month, now.day, START_HOUR, 0, 0)
        target_time = datetime(now.year, now.month, now.day, END_HOUR, 0, 0)
        
        # Check if it's weekend first
        weekday = now.weekday()
        is_weekend = weekday >= 5
        
        if is_weekend:
            return self.make_weekend_panel(now)
        
        # Check if it's break time
        break_type, minutes_left = is_break_time(now)
        if break_type:
            return self.make_break_panel(break_type, minutes_left, now)
        
        # Handle pre-work time
        if now.hour < START_HOUR:
            time_until_work = start_time - now
            hours_until = time_until_work.seconds // 3600
            minutes_until = (time_until_work.seconds % 3600) // 60
            seconds_until = time_until_work.seconds % 60
            
            content = Table.grid(padding=1)
            
        if COFFEE_ART and self.showcase_mode:
            content.add_row(Text(COFFEE_ART, style="dim"))
            
            content.add_row(Text("☕ Cherish this brief moment of freedom before", style="italic", justify="center"))
            content.add_row(Text("the corporate machine claims your soul.", style="italic", justify="center"))
            content.add_row(Text(f"Work begins in: {hours_until:02d}:{minutes_until:02d}:{seconds_until:02d}", 
                               style="bold yellow", justify="center"))
            content.add_row(Text(f"Current time: {now.strftime('%H:%M:%S')}", style="cyan", justify="center"))
            
            return Panel(content, title="PRE-WORK TRANQUILITY", border_style="blue", expand=True)
        
        # Handle post-work time
        elif now.hour >= END_HOUR:
            content = Table.grid(padding=1)
            
            if SUNSET_ART and self.showcase_mode:
                content.add_row(Text(SUNSET_ART, style="dim"))
            
            content.add_row(Text("🌅 Your daily contribution to capitalism is complete.", style="italic green", justify="center"))
            content.add_row(Text("Temporary freedom awaits, until tomorrow's cycle.", style="italic", justify="center"))
            content.add_row(Text(f"Work completed at: {END_HOUR:02d}:00", style="green", justify="center"))
            content.add_row(Text(f"Current time: {now.strftime('%H:%M:%S')}", style="cyan", justify="center"))
            
            return Panel(content, title="POST-WORK LIBERATION", border_style="green", expand=True)
        
        # Handle work hours
        else:
            remaining_time = target_time - now
            hours = math.floor(remaining_time.total_seconds() / 3600)
            minutes = math.floor((remaining_time.total_seconds() % 3600) / 60)
            seconds = math.floor(remaining_time.total_seconds() % 60)
            
            # Calculate progress
            elapsed_time = now - start_time
            total_work_seconds = (END_HOUR - START_HOUR) * 3600
            progress = min(math.floor((elapsed_time.total_seconds() / total_work_seconds) * 100), 100)
            
            # Animation character
            anim_chars = ['|', '/', '-', '\\']
            anim_char = anim_chars[now.second % 4]
            
            # Progress color
            progress_color = "red" if progress < 33 else "yellow" if progress < 66 else "green"
            
            # Select message
            message_index = min(math.floor(progress / 10), len(EXISTENTIAL_MESSAGES) - 1)
            message = EXISTENTIAL_MESSAGES[message_index]
            
            # Next break info
            next_break_info = ""
            current_time = (now.hour, now.minute)
            if current_time < MORNING_BREAK_START:
                time_to_break = (MORNING_BREAK_START[0] - now.hour) * 60 + MORNING_BREAK_START[1] - now.minute
                next_break_info = f"Next break in: {time_to_break} minutes (Morning Break)"
            elif current_time < LUNCH_BREAK_START and current_time >= MORNING_BREAK_END:
                time_to_break = (LUNCH_BREAK_START[0] - now.hour) * 60 + LUNCH_BREAK_START[1] - now.minute
                next_break_info = f"Next break in: {time_to_break} minutes (Lunch)"
            elif current_time < AFTERNOON_BREAK_START and current_time >= LUNCH_BREAK_END:
                time_to_break = (AFTERNOON_BREAK_START[0] - now.hour) * 60 + AFTERNOON_BREAK_START[1] - now.minute
                next_break_info = f"Next break in: {time_to_break} minutes (Afternoon Break)"
            
            content = Table.grid(padding=1)
            content.add_row(Text(f"Progress: {progress}% Complete", style=f"bold {progress_color}", justify="center"))
            content.add_row(Text(f"Remaining: {hours:02d}:{minutes:02d}:{seconds:02d}", style="bold yellow", justify="center"))
            content.add_row(Text(f"Current time: {now.strftime('%H:%M:%S')}", style="cyan", justify="center"))
            content.add_row(Text(f"Status: {anim_char} {message}", style="italic", justify="center"))
            if next_break_info:
                content.add_row(Text(f"☕ {next_break_info}", style="dim cyan", justify="center"))
            
            return Panel(content, title="DAILY CORPORATE SENTENCE", border_style="cyan", expand=True)
    
    def make_layout(self):
        """Create the main layout structure"""
        layout = Layout()
        now = self.get_current_time()
        
        # Calculate header size based on ASCII art
        header_lines = len(ASCII_ART.split('\n')) if ASCII_ART else 3
        header_size = header_lines + 4  # Add padding for panel borders and subtitle
        
        # Create consistent layout for both weekdays and weekends
        layout.split_column(
            Layout(name="header", size=header_size),
            Layout(name="main", ratio=1),  # Main content area
            Layout(name="footer", size=1)
        )
        return layout
    
    def update_display(self, layout):
        """Update the display with current time"""
        now = self.get_current_time()
        is_weekend = now.weekday() >= 5
        
        # Always update header and footer
        layout["header"].update(self.make_header())
        
        footer_text = "Tracking the inevitable passage of time... (Press Ctrl+C to escape)"
        if self.showcase_mode:
            footer_text += " [SHOWCASE MODE]"
        layout["footer"].update(Text(footer_text, style="dim", justify="center"))
        
        # Create main content area split into two columns
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1)
        )
        
        # Left side: Year and Week panels stacked
        layout["main"]["left"].split_column(
            Layout(self.make_year_panel(now)),
            Layout(self.make_week_panel(now))
        )
        
        # Right side: Time/Break/Weekend panel (full height)
        layout["main"]["right"].update(self.make_time_panel(now))

def main():
    """Main function with smooth Rich Live updates"""
    # Parse arguments
    parser = argparse.ArgumentParser(description="The Existential Progress Tracker")
    parser.add_argument('--showcase', action='store_true', help='Run in showcase mode to demo all features')
    args = parser.parse_args()
    
    try:
        if args.showcase:
            console.print("Starting Existential Time Tracker in [bold magenta]SHOWCASE MODE[/bold magenta]...", style="dim")
            console.print("Cycling through all states every 5 seconds...", style="dim")
        else:
            console.print("Starting Existential Time Tracker...", style="dim")
        time.sleep(1)
        
        tracker = ExistentialTracker(showcase_mode=args.showcase)
        layout = tracker.make_layout()
        
        with Live(layout, refresh_per_second=1, screen=True) as live:
            while True:
                tracker.update_display(layout)
                time.sleep(0.1)  # Small sleep to prevent CPU spinning
                
    except KeyboardInterrupt:
        console.print("\n\nProgress tracking interrupted. The corporate overlords are displeased, but your soul is momentarily liberated.", 
                     style="bold red")
        time.sleep(2)
    except Exception as e:
        console.print(f"\nAn error occurred: {e}",style="bold red")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
   main()
