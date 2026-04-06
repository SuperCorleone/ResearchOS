import React from 'react';
import {Box, Text} from 'ink';

/** Pipeline data from backend state_snapshot.research_pipelines */
export interface ResearchPipeline {
	id: string;
	title: string;
	stage: string;
	step: string | null;
	milestone_count: number;
	priority: number;
	budget_used: number;
	budget_allocated: number;
	last_error: string | null;
}

export interface ResearchState {
	pipelines: ResearchPipeline[];
	total_budget_used: number;
	total_budget: number;
	miner_paused: boolean;
	directions: Array<{name: string; priority: string; active: number; total: number}>;
}

const STAGE_ICONS: Record<string, string> = {
	proposal: '\u{1F4CB}',     // 📋
	fast_fail: '\u{26A1}',     // ⚡
	main: '\u{1F52C}',         // 🔬
	milestone: '\u{1F3C1}',    // 🏁
	paper: '\u{1F4DD}',        // 📝
	completed: '\u{2705}',     // ✅
	failed: '\u{274C}',        // ❌
	blocked: '\u{23F8}\u{FE0F}', // ⏸️
};

const STAGE_COLORS: Record<string, string> = {
	proposal: 'yellow',
	fast_fail: 'cyan',
	main: 'blue',
	milestone: 'magenta',
	paper: 'green',
	completed: 'green',
	failed: 'red',
	blocked: 'red',
};

const PRIORITY_LABELS = ['low', 'normal', 'high', 'urgent'];
const PRIORITY_COLORS = ['gray', 'white', 'yellow', 'red'];

export function ResearchDashboard({state}: {state: ResearchState}): React.JSX.Element {
	const {pipelines, total_budget_used, total_budget, miner_paused, directions} = state;

	const active = pipelines.filter(p => !['completed', 'failed'].includes(p.stage));
	const blocked = pipelines.filter(p => p.stage === 'blocked');
	const completed = pipelines.filter(p => p.stage === 'completed');

	// Budget bar
	const pct = total_budget > 0 ? (total_budget_used / total_budget) * 100 : 0;
	const barLen = 30;
	const filled = Math.round(barLen * pct / 100);
	const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(barLen - filled);

	return (
		<Box flexDirection="column" paddingX={1}>
			<Text bold>ResearchOS Pipeline Dashboard</Text>
			<Text dimColor>{'\u2550'.repeat(50)}</Text>

			{/* Budget */}
			<Box>
				<Text>Budget: [{bar}] {total_budget_used}/{total_budget} ({pct.toFixed(0)}%)</Text>
			</Box>

			{/* Counters */}
			<Box gap={2}>
				<Text color="green">{'\u25CF'} {active.length} active</Text>
				<Text dimColor>{'\u25CF'} {completed.length} completed</Text>
				<Text color="red">{'\u25CF'} {blocked.length} blocked</Text>
				<Text> Miner: </Text>
				<Text color={miner_paused ? 'red' : 'green'}>{miner_paused ? 'PAUSED' : 'ACTIVE'}</Text>
			</Box>

			<Text> </Text>

			{/* Pipeline list */}
			{pipelines.map(p => {
				const icon = STAGE_ICONS[p.stage] ?? '  ';
				const color = STAGE_COLORS[p.stage] ?? 'white';
				const prioLabel = PRIORITY_LABELS[p.priority] ?? String(p.priority);
				const prioColor = PRIORITY_COLORS[p.priority] ?? 'white';
				const step = p.step ?? '-';
				const budgetStr = `${p.budget_used}/${p.budget_allocated}`;

				return (
					<Box key={p.id} flexDirection="column">
						<Box gap={1}>
							<Text>{icon}</Text>
							<Text color={color as any}>{p.id.substring(0, 28).padEnd(28)}</Text>
							<Text color={color as any}>{p.stage.padEnd(12)}</Text>
							<Text>{step.padEnd(15)}</Text>
							<Text>M{p.milestone_count}</Text>
							<Text color={prioColor as any}>{prioLabel.padEnd(7)}</Text>
							<Text dimColor>{budgetStr}</Text>
						</Box>
						{p.stage !== 'completed' && p.stage !== 'failed' && (
							<Box paddingLeft={4}>
								<Text dimColor>{p.title.substring(0, 60)}</Text>
							</Box>
						)}
						{p.last_error && (
							<Box paddingLeft={4}>
								<Text color="red">{p.last_error.substring(0, 60)}</Text>
							</Box>
						)}
					</Box>
				);
			})}

			{pipelines.length === 0 && (
				<Text dimColor>No pipelines. Use /research create or /research inject to start.</Text>
			)}

			<Text> </Text>

			{/* Research directions */}
			{directions.length > 0 && (
				<>
					<Text bold>Research Directions</Text>
					{directions.map(d => (
						<Box key={d.name} gap={1} paddingLeft={2}>
							<Text color={d.priority === 'high' ? 'yellow' : d.priority === 'medium' ? 'white' : 'gray'}>
								[{d.priority.padStart(6)}]
							</Text>
							<Text>{d.name}: {d.active} active / {d.total} total</Text>
						</Box>
					))}
				</>
			)}

			<Text> </Text>
			<Text dimColor>Commands: /research dashboard | create | fix | publish | priority | launch</Text>
		</Box>
	);
}
