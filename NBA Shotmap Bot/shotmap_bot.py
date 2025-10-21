import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np
import pandas as pd
import requests
from nba_api.stats.endpoints import shotchartdetail, playercareerstats
from nba_api.stats.static import players
import os


# ---------- Scraper ----------
class NbaScraper:
    @staticmethod
    def get_player_json(name: str):
        nba_players = players.get_players()
        matches = [p for p in nba_players if p['full_name'].lower() == name.lower()]
        if not matches:
            raise ValueError(f"Player '{name}' not found.")
        return matches[0]

    @staticmethod
    def get_player_career(player_id: int):
        career = playercareerstats.PlayerCareerStats(player_id=player_id)
        return career.get_data_frames()[0]

    @staticmethod
    def get_shot_data(player_id: int, team_ids: list, seasons: list, playoffs=False):
        df = pd.DataFrame()
        season_type = "Playoffs" if playoffs else "Regular Season"
        for season in seasons:
            for team in team_ids:
                shot_data = shotchartdetail.ShotChartDetail(
                    team_id=team,
                    player_id=player_id,
                    context_measure_simple='FGA',
                    season_type_all_star=season_type,
                    season_nullable=season
                )
                df = pd.concat([df, shot_data.get_data_frames()[0]])
        return df


# ---------- Chart Builder ----------
class ShotCharts:
    @staticmethod
    def create_court(ax: mpl.axes.Axes, color="white"):
        lw = 2.3
        ax.plot([-220, -220], [0, 140], lw=lw, color=color)
        ax.plot([220, 220], [0, 140], lw=lw, color=color)
        ax.add_artist(mpl.patches.Arc((0, 140), 440, 315, theta1=0, theta2=180, color=color, lw=lw))
        ax.plot([-80, -80], [0, 190], lw=lw, color=color)
        ax.plot([80, 80], [0, 190], lw=lw, color=color)
        ax.plot([-60, -60], [0, 190], lw=lw, color=color)
        ax.plot([60, 60], [0, 190], lw=lw, color=color)
        ax.plot([-80, 80], [190, 190], lw=lw, color=color)
        ax.add_artist(mpl.patches.Circle((0, 190), 60, fill=False, color=color, lw=lw))
        ax.plot([-250, 250], [0, 0], lw=lw + 0.5, color=color)
        ax.add_artist(mpl.patches.Circle((0, 60), 15, fill=False, color=color, lw=lw))
        ax.plot([-30, 30], [40, 40], lw=lw, color=color)
        ax.set_xlim(-250, 250)
        ax.set_ylim(0, 470)
        ax.axis("off")
        return ax

    @staticmethod
    def add_headshot(fig: plt.Figure, player_id: int):
        try:
            url = f"https://ak-static.cms.nba.com/wp-content/uploads/headshots/nba/latest/260x190/{player_id}.png"
            im = plt.imread(requests.get(url, stream=True).raw)
            ax = fig.add_axes([0.06, 0.01, 0.3, 0.3])
            ax.imshow(im)
            ax.axis("off")
        except Exception:
            pass
        return fig

    # --- Shot Volume ---
    @staticmethod
    def volume_chart(df, name, seasons, RA=True, playoffs=False):
        extent = (-250, 250, -47.5, 422.5)
        gridsize = 25
        cmap = "plasma"

        fig = plt.figure(figsize=(4, 4), facecolor='black')
        ax = fig.add_axes([0, 0, 1, 1], facecolor='black')

        # Filter for restricted area
        if RA:
            x, y = df.LOC_X, df.LOC_Y + 60
        else:
            cond = ~((-45 < df.LOC_X) & (df.LOC_X < 45) & (-40 < df.LOC_Y) & (df.LOC_Y < 45))
            x, y = df.LOC_X[cond], df.LOC_Y[cond] + 60

        # Brighter density scaling
        hb = ax.hexbin(x, y, cmap=cmap, bins="log", gridsize=gridsize, mincnt=2, extent=extent)
        c = hb.get_array()
        if len(c) > 0:
            hb.set_array(np.power(c / np.max(c), 0.6) * np.max(c))  # brighten midtones

        ShotCharts.create_court(ax, 'white')

        plt.text(-250, 440, f"{name.title()}", fontsize=19, color='white', fontname='Franklin Gothic Medium')
        plt.text(-250, 410, "Shot Volume", fontsize=12, color='white', fontname='Franklin Gothic Book')
        if not RA:
            plt.text(-250, 390, "(w/o restricted area)", fontsize=10, color='red', fontname='Franklin Gothic Book')
        season_str = f"{seasons[0][:4]}-{seasons[-1][-2:]}"
        plt.text(-250, -20, f"{season_str} {'Playoffs' if playoffs else 'Regular Season'}", fontsize=8, color='white')
        plt.text(110, -20, '@hotshot_nba', fontsize=8, color='white')

        # --- Add Kaggle colorbar overlay ---
        try:
            im = plt.imread("https://github.com/ubiratanfilho/HotShot/blob/main/images/Colorbar%20Shotcharts.png?raw=true")
            newax = fig.add_axes([0.56, 0.6, 0.45, 0.4], anchor='NE', zorder=1)
            newax.imshow(im)
            newax.axis("off")
        except Exception:
            pass

        ShotCharts.add_headshot(fig, df.PLAYER_ID.iloc[0])
        return fig

    # --- Frequency Chart ---
    @staticmethod
    def frequency_chart(df, name, seasons, playoffs=False):
        extent = (-250, 250, -47.5, 422.5)
        gridsize = 25
        cmap = "inferno"

        # Base heat map
        shots_hex = plt.hexbin(df.LOC_X, df.LOC_Y + 60, extent=extent, cmap=cmap, gridsize=gridsize)
        plt.close()
        freq_by_hex = shots_hex.get_array() / sum(shots_hex.get_array())

        makes_df = df[df.SHOT_MADE_FLAG == 1]
        makes_hex = plt.hexbin(makes_df.LOC_X, makes_df.LOC_Y + 60, cmap=cmap, gridsize=gridsize, extent=extent)
        plt.close()
        pcts_by_hex = makes_hex.get_array() / shots_hex.get_array()
        pcts_by_hex[np.isnan(pcts_by_hex)] = 0

        x = [i[0] for i in shots_hex.get_offsets()]
        y = [i[1] for i in shots_hex.get_offsets()]
        z = pcts_by_hex
        sizes = freq_by_hex * 1100  # bigger + more readable points

        fig = plt.figure(figsize=(4, 4), facecolor='black')
        ax = fig.add_axes([0, 0, 1, 1], facecolor='black')
        plt.xlim(250, -250)
        plt.ylim(-47.5, 422.5)

        scatter = ax.scatter(x, y, c=z, cmap=cmap, marker='h', s=sizes, alpha=0.9, edgecolor='none')
        ShotCharts.create_court(ax)

        legend_acc = plt.legend(*scatter.legend_elements(num=5, fmt="{x:.0f}%", func=lambda x: x * 100),
                                loc=[0.83,0.77], title='Shot %', fontsize=6)
        legend_freq = plt.legend(*scatter.legend_elements('sizes', num=5, alpha=0.8, fmt="{x:.1f}%",
                                func=lambda s: s / max(sizes) * max(freq_by_hex) * 100),
                                loc=[0.66,0.77], title='Freq %', fontsize=6)
        plt.gca().add_artist(legend_acc)

        plt.text(-250, 450, f"{name.title()}", fontsize=18, color='white')
        plt.text(-250, 420, "Frequency and FG%", fontsize=10, color='white')
        season_str = f"{seasons[0][:4]}-{seasons[-1][-2:]}"
        plt.text(-250, -20, f"{season_str}", fontsize=8, color='white')
        plt.text(110, -20, '@hotshot_nba', fontsize=8, color='white')

        ShotCharts.add_headshot(fig, df.PLAYER_ID.iloc[0])
        return fig

    # --- Makes & Misses Chart ---
    @staticmethod
    def makes_misses_chart(df, name, seasons, playoffs=False):
        fig = plt.figure(figsize=(4, 4), facecolor='black')
        ax = fig.add_axes([0, 0, 1, 1], facecolor='black')

        plt.text(-250, 450, f"{name.title()}", fontsize=18, color='white')
        plt.text(-250, 425, "Misses", fontsize=12, color='red')
        plt.text(-170, 425, "&", fontsize=12, color='white')
        plt.text(-150, 425, "Buckets", fontsize=12, color='green')

        season_str = f"{seasons[0][:4]}-{seasons[-1][-2:]}"
        plt.text(-250, -20, f"{season_str} {'Playoffs' if playoffs else 'Regular Season'}", fontsize=8, color='white')
        plt.text(110, -20, '@hotshot_nba', fontsize=8, color='white')

        ShotCharts.create_court(ax, 'white')
        ax.scatter(df.LOC_X, df.LOC_Y + 60, c=df.SHOT_MADE_FLAG, cmap='RdYlGn', s=15, alpha=0.8)
        ShotCharts.add_headshot(fig, df.PLAYER_ID.iloc[0])
        return fig


# ---------- Generate all four charts ----------
def generate_all_shotmaps(player_name: str, year: str, playoffs=False):
    player = NbaScraper.get_player_json(player_name)
    pid = player['id']
    career = NbaScraper.get_player_career(pid)
    team_ids = list(set(career.TEAM_ID.values))
    season = f"{int(year)-1}-{str(year)[-2:]}"
    df = NbaScraper.get_shot_data(pid, team_ids, [season], playoffs)

    if df.empty:
        raise ValueError("No shot data found for that season.")

    os.makedirs("assets", exist_ok=True)

    charts = {
        "volume": ShotCharts.volume_chart(df, player_name, [season], RA=True, playoffs=playoffs),
        "volume_noRA": ShotCharts.volume_chart(df, player_name, [season], RA=False, playoffs=playoffs),
        "frequency": ShotCharts.frequency_chart(df, player_name, [season], playoffs),
        "makes_misses": ShotCharts.makes_misses_chart(df, player_name, [season], playoffs)
    }

    paths = []
    for name, fig in charts.items():
        path = f"assets/{player_name.replace(' ', '_')}_{year}_{name}.png"
        fig.savefig(path, bbox_inches="tight", facecolor='black', dpi=150)
        plt.close(fig)
        paths.append(path)

    return paths, pid
