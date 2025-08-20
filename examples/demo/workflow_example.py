import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow

async def workflow_example():
    # Create specialized agents with detailed expertise
    market_researcher = Agent(
        name="MarketResearcher",
        system_prompt="""You are a senior market research analyst with 10+ years of experience. 
        Your expertise includes:
        - Industry trend analysis and forecasting
        - Competitive landscape assessment
        - Market size estimation and growth projections
        - Consumer behavior analysis
        - Technology adoption patterns
        
        Always provide data-driven insights with specific metrics, sources, and actionable recommendations.""",
        model="gpt-4o"
    )
    
    data_scientist = Agent(
        name="DataScientist", 
        system_prompt="""You are a senior data scientist specializing in business intelligence and predictive analytics.
        Your core competencies:
        - Statistical analysis and hypothesis testing
        - Predictive modeling and machine learning
        - Data visualization and storytelling
        - Risk assessment and scenario planning
        - Performance metrics and KPI development
        
        Transform raw research into quantitative insights, identify patterns, and build predictive models.""",
        model="gpt-4o"
    )
    
    business_writer = Agent(
        name="BusinessWriter",
        system_prompt="""You are an executive business writer and strategic communications expert.
        Your specializations:
        - Executive summary creation
        - Strategic recommendation development
        - Stakeholder communication
        - Risk and opportunity assessment
        - Implementation roadmap design
        
        Create compelling, actionable business reports that drive decision-making at the C-level.""",
        model="gpt-4o"
    )
    
    financial_analyst = Agent(
        name="FinancialAnalyst",
        system_prompt="""You are a CFA-certified financial analyst with expertise in valuation and investment analysis.
        Your focus areas:
        - Financial modeling and valuation
        - Investment risk assessment
        - ROI and NPV calculations
        - Capital allocation strategies
        - Market opportunity sizing
        
        Provide detailed financial analysis with concrete numbers, projections, and investment recommendations.""",
        model="gpt-4o"
    )
    
    strategy_consultant = Agent(
        name="StrategyConsultant",
        system_prompt="""You are a senior strategy consultant from a top-tier consulting firm.
        Your expertise includes:
        - Strategic planning and execution
        - Business model innovation
        - Competitive strategy development
        - Organizational transformation
        - Change management
        
        Synthesize complex information into clear strategic recommendations with implementation timelines.""",
        model="gpt-4o"
    )
    
    workflow = Workflow()
    
    # Sequential workflow - Research to Analysis to Report Pipeline
    result = await workflow.run_sequential(
        agents=[market_researcher, data_scientist, business_writer],
        task="Analyze the electric vehicle charging infrastructure market opportunity in North America for 2024-2027"
    )
    print("Sequential Pipeline Result:", result.result)
    
    # Parallel workflow - Multiple expert perspectives on same challenge
    result = await workflow.run_parallel(
        agents=[financial_analyst, strategy_consultant, data_scientist],
        task="Evaluate the investment potential and strategic implications of generative AI adoption in enterprise software companies"
    )
    print("Expert Panel Analysis:", result.result)
    
    # Graph workflow - Complex dependency analysis
    dependencies = "MarketResearcher->DataScientist, MarketResearcher->FinancialAnalyst, DataScientist&FinancialAnalyst->StrategyConsultant, StrategyConsultant->BusinessWriter"
    result = await workflow.run_graph(
        agents=[market_researcher, data_scientist, financial_analyst, strategy_consultant, business_writer],
        dependencies=dependencies,
        task="Develop a comprehensive go-to-market strategy for a B2B SaaS startup entering the healthcare analytics space"
    )
    print("Strategic Analysis Result:", result.result)
    
    # Hybrid workflow - Multi-stage comprehensive business analysis
    quality_reviewer = Agent(
        name="QualityReviewer",
        system_prompt="""You are a senior partner-level consultant specializing in quality assurance and risk management.
        Your responsibilities:
        - Strategic recommendation validation
        - Risk identification and mitigation
        - Stakeholder impact assessment
        - Implementation feasibility review
        - Compliance and regulatory considerations
        
        Ensure all recommendations are practical, well-researched, and aligned with business objectives.""",
        model="gpt-4o"
    )
    
    stages = [
        {
            "pattern": "sequential",
            "agents": [market_researcher, financial_analyst],
            "task": "Conduct market and financial analysis for: {original_task}",
            "name": "market_financial_analysis"
        },
        {
            "pattern": "parallel", 
            "agents": [data_scientist, strategy_consultant],
            "task": "Analyze strategic implications and develop data-driven insights based on: {previous_result}",
            "name": "strategic_data_analysis"
        },
        {
            "pattern": "graph",
            "agents": [business_writer, quality_reviewer, strategy_consultant],
            "dependencies": "BusinessWriter->QualityReviewer, StrategyConsultant->QualityReviewer",
            "task": "Create final strategic report with quality validation from: {previous_result}",
            "name": "report_synthesis_validation"
        }
    ]
    
    result = await workflow.run_hybrid(
        task="Develop a digital transformation strategy for a traditional manufacturing company looking to implement IoT and predictive maintenance solutions",
        stages=stages
    )
    print("Comprehensive Strategy Report:", result["final_result"])

asyncio.run(workflow_example())